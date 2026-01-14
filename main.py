import os
import threading
import io
import csv
from flask import Flask
import discord
from discord.ext import commands, tasks
from sqlalchemy import create_engine, Column, String, Boolean, DateTime, Integer
from sqlalchemy.orm import sessionmaker, declarative_base
import datetime

# --- DB設定 ---
DATABASE_URL = os.environ.get("DATABASE_URL")
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_recycle=300, connect_args={'sslmode':'require'})
Session = sessionmaker(bind=engine)
Base = declarative_base()

class MasterOrg(Base):
    __tablename__ = 'master_org_v16'
    id = Column(Integer, primary_key=True, autoincrement=True)
    org_name = Column(String, unique=True, nullable=False)
    alias = Column(String, index=True)
    exclude_leader = Column(Boolean, default=False)
    skip_channel = Column(Boolean, default=False)

class ServerConfig(Base):
    __tablename__ = 'server_config_v16'
    guild_id = Column(String, primary_key=True)
    category_name = Column(String, default="団体用")
    leader_role_name = Column(String, default="部長")
    proxy_role_name = Column(String, default="代理")
    admin_log_channel = Column(String, default="管理ログ")
    target_vc_name = Column(String, default=None)

class VCState(Base):
    __tablename__ = 'vc_history_v16'
    id = Column(Integer, primary_key=True, autoincrement=True)
    guild_id = Column(String)
    user_id = Column(String)
    user_name = Column(String)
    channel_name = Column(String)
    joined_at = Column(DateTime, default=lambda: datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))))
    left_at = Column(DateTime, nullable=True)

Base.metadata.create_all(engine)

def get_config(guild_id):
    session = Session()
    try:
        conf = session.query(ServerConfig).filter_by(guild_id=str(guild_id)).first()
        if not conf:
            conf = ServerConfig(guild_id=str(guild_id))
            session.add(conf); session.commit(); session.refresh(conf)
        return conf
    finally: session.close()

def fetch_all_orgs():
    session = Session()
    try: return session.query(MasterOrg).all()
    finally: session.close()

async def find_best_category(guild, category_names, target_ch_name):
    cat_list = [c.strip() for c in category_names.split(',')]
    existing_ch = discord.utils.get(guild.text_channels, name=target_ch_name)
    if existing_ch and existing_ch.category and existing_ch.category.name in cat_list:
        return existing_ch.category
    for name in cat_list:
        cat = discord.utils.get(guild.categories, name=name)
        if cat and len(cat.channels) < 50: return cat
        if not cat: return await guild.create_category(name)
    return discord.utils.get(guild.categories, name=cat_list[-1])

async def core_sync_logic(user, guild, all_orgs):
    if user.bot: return None
    dn = user.display_name
    found = [o for o in all_orgs if o.org_name.lower() in dn.lower() or (o.alias and o.alias.lower() in dn.lower())]
    conf = get_config(guild.id)
    if len(found) > 1: return f"🚫 {dn}: 重複検知"
    if not found: return f"⚠️ {dn}: 団体名なし"
    target = found[0]
    l_name = None if conf.leader_role_name in ["なし","None","none"] else conf.leader_role_name
    p_name = None if conf.proxy_role_name in ["なし","None","none"] else conf.proxy_role_name
    clean = [o.org_name for o in all_orgs]
    if l_name: clean.append(l_name)
    if p_name: clean.append(p_name)
    to_rem = [r for r in user.roles if r.name in clean and r.name != target.org_name]
    if to_rem: await user.remove_roles(*to_rem)
    o_role = discord.utils.get(guild.roles, name=target.org_name) or await guild.create_role(name=target.org_name, mentionable=True)
    if o_role not in user.roles: await user.add_roles(o_role)
    if p_name and (p_name in dn or "代理" in dn):
        p_role = discord.utils.get(guild.roles, name=p_name) or await guild.create_role(name=p_name)
        await user.add_roles(p_role)
    elif l_name and not target.exclude_leader:
        l_role = discord.utils.get(guild.roles, name=l_name) or await guild.create_role(name=l_name)
        await user.add_roles(l_role)
    if target.skip_channel: return f"✅ {dn} 同期完了"
    ch_n = target.org_name.lower().replace(" ", "-")
    cat = await find_best_category(guild, conf.category_name, ch_n)
    chan = discord.utils.get(guild.text_channels, name=ch_n)
    ow = {guild.default_role: discord.PermissionOverwrite(read_messages=False),
          o_role: discord.PermissionOverwrite(read_messages=True, send_messages=True),
          guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True)}
    if not chan: await guild.create_text_channel(ch_n, category=cat, overwrites=ow)
    else:
        if chan.category != cat: await chan.edit(category=cat)
        await chan.edit(overwrites=ow)
    return f"✅ {dn} 同期完了"

bot = commands.Bot(command_prefix='!', intents=discord.Intents.all())

@tasks.loop(time=datetime.time(hour=12, minute=0, tzinfo=datetime.timezone(datetime.timedelta(hours=9))))
async def scheduled_sync():
    all_o = fetch_all_orgs()
    for g in bot.guilds:
        async for m in g.fetch_members(limit=None): await core_sync_logic(m, g, all_o)

@bot.event
async def on_ready():
    bot.add_view(MultiFunctionView())
    if not scheduled_sync.is_running(): scheduled_sync.start()
    print("✅ Online")
    for g in bot.guilds:
        c = get_config(g.id)
        # チャンネル名で検索。見つからない場合はスルー
        target_ch = discord.utils.get(g.text_channels, name=c.admin_log_channel)
        if target_ch:
            try:
                # 過去のパネル（自分自身の投稿かつ特定文言を含むもの）を削除
                async for m in target_ch.history(limit=20):
                    if m.author == bot.user and "統合管理パネル" in m.content:
                        await m.delete()
                # 最新のパネルを送信
                await target_ch.send(f"**【{g.name} 統合管理パネル】**", view=MultiFunctionView())
            except Exception as e:
                print(f"パネル送信失敗 ({g.name}): {e}")

@bot.event
async def on_voice_state_update(m, b, a):
    if m.bot or b.channel == a.channel: return
    c = get_config(m.guild.id)
    if not c.target_vc_name: return
    s = Session()
    try:
        now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
        if b.channel and b.channel.name == c.target_vc_name:
            if not a.channel or a.channel.id != b.channel.id:
                rec = s.query(VCState).filter_by(user_id=str(m.id), guild_id=str(m.guild.id), channel_name=c.target_vc_name, left_at=None).order_by(VCState.joined_at.desc()).first()
                if rec:
                    rec.left_at = now
                    s.commit()
        if a.channel and a.channel.name == c.target_vc_name:
            existing = s.query(VCState).filter_by(user_id=str(m.id), guild_id=str(m.guild.id), channel_name=c.target_vc_name, left_at=None).first()
            if not existing:
                s.add(VCState(guild_id=str(m.guild.id), user_id=str(m.id), user_name=m.display_name, channel_name=a.channel.name))
                s.commit()
    except Exception as e:
        print(f"VCログエラー: {e}"); s.rollback()
    finally: s.close()

@bot.command()
@commands.has_permissions(administrator=True)
async def vc_sync(ctx):
    conf = get_config(ctx.guild.id)
    if not conf.target_vc_name: return await ctx.send("❌ 監視VC未設定。")
    vc = discord.utils.get(ctx.guild.voice_channels, name=conf.target_vc_name)
    if not vc: return await ctx.send(f"❌ VC『{conf.target_vc_name}』なし。")
    members = [m.display_name for m in vc.members]
    txt = f"📊 **VC出席確認 ({vc.name})**\n人数: {len(members)}名\n```\n" + (", ".join(members) if members else "なし") + "\n```"
    await ctx.send(txt)

@bot.command()
@commands.has_permissions(administrator=True)
async def panel(ctx):
    await ctx.send(f"**【{ctx.guild.name} 統合管理パネル】**", view=MultiFunctionView())

@bot.command()
@commands.has_permissions(administrator=True)
async def set_config(ctx, cat, leader, proxy, log):
    s = Session(); conf = s.query(ServerConfig).filter_by(guild_id=str(ctx.guild.id)).first()
    if not conf: conf = ServerConfig(guild_id=str(ctx.guild.id)); s.add(conf)
    conf.category_name = cat; conf.leader_role_name = leader; conf.proxy_role_name = proxy; conf.admin_log_channel = log
    s.commit(); s.close()
    await ctx.send(f"✅ 設定更新完了")

@bot.command()
@commands.has_permissions(administrator=True)
async def set_vc(ctx, vc_name):
    s = Session(); conf = s.query(ServerConfig).filter_by(guild_id=str(ctx.guild.id)).first()
    if not conf: conf = ServerConfig(guild_id=str(ctx.guild.id)); s.add(conf)
    conf.target_vc_name = vc_name; s.commit(); s.close()
    await ctx.send(f"✅ 監視VC設定: {vc_name}")

@bot.command()
@commands.has_permissions(administrator=True)
async def add_orgs(ctx, *, data: str):
    s = Session()
    try:
        for l in data.strip().split('\n'):
            p = l.split()
            if not p: continue
            try:
                o = MasterOrg(org_name=p[0], alias=p[1] if len(p)>1 else None, exclude_leader=p[2].lower()=='true' if len(p)>2 else False, skip_channel=p[3].lower()=='true' if len(p)>3 else False)
                s.add(o); s.commit()
            except: s.rollback()
        await ctx.send(f"✅ 登録完了")
    finally: s.close()

@bot.command()
@commands.has_permissions(administrator=True)
async def del_org(ctx, name: str):
    s = Session(); t = s.query(MasterOrg).filter_by(org_name=name).first()
    if t: s.delete(t); s.commit(); await ctx.send(f"✅ {name} 削除")
    else: await ctx.send("なし")
    s.close()

@bot.command()
@commands.has_permissions(administrator=True)
async def export_vc(ctx):
    s = Session(); h = s.query(VCState).filter_by(guild_id=str(ctx.guild.id)).all()
    o = io.StringIO(); w = csv.writer(o); w.writerow(["ユーザー", "VC", "入室", "退出"])
    for x in h: w.writerow([x.user_name, x.channel_name, x.joined_at, x.left_at or "中"])
    o.seek(0); await ctx.send(file=discord.File(io.BytesIO(o.getvalue().encode()), filename="vc.csv")); s.close()

@bot.command()
async def sync(ctx):
    res = await core_sync_logic(ctx.author, ctx.guild, fetch_all_orgs())
    if res: await ctx.send(res)

class MultiFunctionView(discord.ui.View):
    def __init__(self): super().__init__(timeout=None)
    @discord.ui.button(label="一括同期", style=discord.ButtonStyle.danger, custom_id="sync_all")
    async def sync_all(self, interaction, button):
        if not interaction.user.guild_permissions.administrator: return
        await interaction.response.send_message("同期中...", ephemeral=True)
        all_o = fetch_all_orgs(); count = 0
        async for m in interaction.guild.fetch_members(limit=None):
            res = await core_sync_logic(m, interaction.guild, all_o)
            if res and "✅" in res: count += 1
        await interaction.followup.send(f"完了: {count}名")

app = Flask(__name__)
@app.route('/')
def home(): return "OK", 200
def run_flask(): app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000)))

if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    bot.run(os.environ.get("DISCORD_TOKEN"))