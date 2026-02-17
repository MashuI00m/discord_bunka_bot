import os, threading, io, csv, datetime, asyncio, time
from flask import Flask
import discord
from discord.ext import commands, tasks
from sqlalchemy import create_engine, Column, String, Boolean, DateTime, Integer
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy.exc import OperationalError

# --- DB設定 (オレゴン↔日本間の遅延対策) ---
DATABASE_URL = os.environ.get("DATABASE_URL", "").replace("postgres://", "postgresql://", 1)
engine = create_engine(
    DATABASE_URL, 
    pool_pre_ping=True, 
    pool_recycle=300, 
    pool_timeout=60,
    connect_args={'connect_timeout': 10}
)
Session = sessionmaker(bind=engine)
Base = declarative_base()

# --- 各テーブル定義 ---
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

class AttendanceLog(Base):
    __tablename__ = 'attendance_log_v16'
    id = Column(Integer, primary_key=True, autoincrement=True)
    guild_id = Column(String); user_id = Column(String); user_name = Column(String); org_name = Column(String); status = Column(String)
    timestamp = Column(DateTime, default=lambda: datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))))

class VCState(Base):
    __tablename__ = 'vc_history_v16'
    id = Column(Integer, primary_key=True, autoincrement=True)
    guild_id = Column(String); user_id = Column(String); user_name = Column(String); channel_name = Column(String)
    joined_at = Column(DateTime, default=lambda: datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))))
    left_at = Column(DateTime, nullable=True)

Base.metadata.create_all(engine)

# --- 共通ユーティリティ ---
def get_config_safe(guild_id):
    session = Session()
    try:
        conf = session.query(ServerConfig).filter_by(guild_id=str(guild_id)).first()
        if not conf:
            conf = ServerConfig(guild_id=str(guild_id))
            session.add(conf); session.commit(); session.refresh(conf)
        return conf
    except: return ServerConfig(guild_id=str(guild_id))
    finally: session.close()

def fetch_all_orgs():
    session = Session()
    try: return session.query(MasterOrg).all()
    except: return []
    finally: session.close()

async def find_best_category(guild, category_names, target_ch_name):
    cat_list = [c.strip() for c in category_names.split(',')]
    for name in cat_list:
        cat = discord.utils.get(guild.categories, name=name)
        if cat and len(cat.channels) < 50: return cat
        if not cat: return await guild.create_category(name)
    return discord.utils.get(guild.categories, name=cat_list[-1])

async def core_sync_logic(user, guild, all_orgs):
    if user.bot: return None
    dn = user.display_name; found = [o for o in all_orgs if o.org_name.lower() in dn.lower() or (o.alias and o.alias.lower() in dn.lower())]
    if len(found) != 1: return None
    conf = get_config_safe(guild.id); target = found[0]
    l_n = None if conf.leader_role_name in ["なし", "none"] else conf.leader_role_name
    p_n = None if conf.proxy_role_name in ["なし", "none"] else conf.proxy_role_name
    clean = [o.org_name for o in all_orgs] + ([l_n] if l_n else []) + ([p_n] if p_n else [])
    await user.remove_roles(*[r for r in user.roles if r.name in clean and r.name != target.org_name])
    o_role = discord.utils.get(guild.roles, name=target.org_name) or await guild.create_role(name=target.org_name, mentionable=True)
    if o_role not in user.roles: await user.add_roles(o_role)
    if p_n and (p_n in dn or "代理" in dn):
        await user.add_roles(discord.utils.get(guild.roles, name=p_n) or await guild.create_role(name=p_n))
    elif l_n and not target.exclude_leader:
        await user.add_roles(discord.utils.get(guild.roles, name=l_n) or await guild.create_role(name=l_n))
    if not target.skip_channel:
        ch_n = target.org_name.lower().replace(" ", "-"); cat = await find_best_category(guild, conf.category_name, ch_n); chan = discord.utils.get(guild.text_channels, name=ch_n)
        ow = {guild.default_role: discord.PermissionOverwrite(read_messages=False), o_role: discord.PermissionOverwrite(read_messages=True), guild.me: discord.PermissionOverwrite(read_messages=True)}
        if not chan: await guild.create_text_channel(ch_n, category=cat, overwrites=ow)
        else:
            if chan.category != cat: await chan.edit(category=cat)
            await chan.edit(overwrites=ow)
    return True

async def get_combined_report(guild, mode="button"):
    s = Session()
    try:
        all_o = s.query(MasterOrg).all(); conf = get_config_safe(guild.id); target_user_ids = set()
        if mode == "button":
            t = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).replace(hour=0, minute=0, second=0, microsecond=0)
            target_user_ids = {l.user_id for l in s.query(AttendanceLog).filter(AttendanceLog.guild_id == str(guild.id), AttendanceLog.timestamp >= t).all()}
            title = "📋 **出席レポート (ボタン)**"
        else:
            vc = discord.utils.get(guild.voice_channels, name=conf.target_vc_name); target_user_ids = {str(m.id) for m in vc.members} if vc else set()
            title = f"🎙️ **VC出席レポート ({conf.target_vc_name or '未設定'})**"
        res = f"{title}\n\n**出席状況:**\n"
        for o in all_o:
            r = discord.utils.get(guild.roles, name=o.org_name)
            p = [m.display_name for m in r.members if str(m.id) in target_user_ids] if r else []
            res += f"{o.org_name}: {', '.join(p) if p else '不参加'}\n"
        return res
    finally: s.close()

# --- Bot本体 ---
bot = commands.Bot(command_prefix='!', intents=discord.Intents.all())

@tasks.loop(time=datetime.time(hour=12, minute=0, tzinfo=datetime.timezone(datetime.timedelta(hours=9))))
async def scheduled_sync():
    all_o = fetch_all_orgs()
    for g in bot.guilds:
        count = 0
        async for m in g.fetch_members(limit=None):
            if await core_sync_logic(m, g, all_o): count += 1
            await asyncio.sleep(0.1)
        conf = get_config_safe(g.id); ch = discord.utils.get(g.text_channels, name=conf.admin_log_channel)
        if ch: await ch.send(f"🕛 定時自動同期完了: {count}名を更新。")

@bot.event
async def on_ready():
    bot.add_view(MultiFunctionView())
    if not scheduled_sync.is_running(): scheduled_sync.start()
    print(f"✅ Bot Online: {bot.user}")
    await asyncio.sleep(10)
    for g in bot.guilds:
        c = get_config_safe(g.id); ch = discord.utils.get(g.text_channels, name=c.admin_log_channel)
        if ch:
            async for m in ch.history(limit=5):
                if m.author == bot.user and "統合管理パネル" in m.content: await m.delete()
            await ch.send(f"**【{g.name} 統合管理パネル】**", view=MultiFunctionView())

@bot.event
async def on_voice_state_update(m, b, a):
    if m.bot or b.channel == a.channel: return
    c = get_config_safe(m.guild.id); s = Session(); now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    try:
        if b.channel and b.channel.name == c.target_vc_name:
            rec = s.query(VCState).filter_by(user_id=str(m.id), guild_id=str(m.guild.id), channel_name=c.target_vc_name, left_at=None).order_by(VCState.joined_at.desc()).first()
            if rec: rec.left_at = now; s.commit()
        if a.channel and a.channel.name == c.target_vc_name:
            if not s.query(VCState).filter_by(user_id=str(m.id), guild_id=str(m.guild.id), channel_name=c.target_vc_name, left_at=None).first():
                s.add(VCState(guild_id=str(m.guild.id), user_id=str(m.id), user_name=m.display_name, channel_name=a.channel.name)); s.commit()
    except: s.rollback()
    finally: s.close()

# --- 管理コマンド完全復旧 ---
@bot.command()
@commands.has_permissions(administrator=True)
async def report(ctx): await ctx.send(await get_combined_report(ctx.guild, mode="button"))

@bot.command()
@commands.has_permissions(administrator=True)
async def report_vc(ctx): await ctx.send(await get_combined_report(ctx.guild, mode="vc"))

@bot.command()
@commands.has_permissions(administrator=True)
async def report_reset(ctx):
    s = Session(); t = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).replace(hour=0, minute=0, second=0, microsecond=0)
    s.query(AttendanceLog).filter(AttendanceLog.guild_id == str(ctx.guild.id), AttendanceLog.timestamp >= t).delete()
    s.commit(); s.close(); await ctx.send("✅ 本日の出席ログをリセットしました。")

@bot.command()
@commands.has_permissions(administrator=True)
async def list_orgs(ctx):
    all_o = fetch_all_orgs(); m = "📋 **登録団体一覧**\n" + "\n".join([f"・{o.org_name} (略称: {o.alias or 'なし'})" for o in all_o])
    await ctx.send(m if all_o else "団体が登録されていません。")

@bot.command()
@commands.has_permissions(administrator=True)
async def add_orgs(ctx, *, data: str):
    s = Session()
    for l in data.strip().split('\n'):
        p = l.split()
        if p: s.merge(MasterOrg(org_name=p[0], alias=p[1] if len(p)>1 else None, exclude_leader=p[2].lower()=='true' if len(p)>2 else False, skip_channel=p[3].lower()=='true' if len(p)>3 else False))
    s.commit(); s.close(); await ctx.send("✅ 団体情報を追加/更新しました。")

@bot.command()
@commands.has_permissions(administrator=True)
async def del_org(ctx, name: str):
    s = Session(); t = s.query(MasterOrg).filter_by(org_name=name).first()
    if t: s.delete(t); s.commit(); await ctx.send(f"✅ 「{name}」を削除しました。")
    else: await ctx.send("該当する団体が見つかりません。")
    s.close()

@bot.command()
@commands.has_permissions(administrator=True)
async def set_config(ctx, cat, l, p, log):
    s = Session(); conf = s.query(ServerConfig).filter_by(guild_id=str(ctx.guild.id)).first() or ServerConfig(guild_id=str(ctx.guild.id)); s.add(conf)
    conf.category_name, conf.leader_role_name, conf.proxy_role_name, conf.admin_log_channel = cat, l, p, log
    s.commit(); s.close(); await ctx.send("✅ サーバー設定を更新しました。")

@bot.command()
@commands.has_permissions(administrator=True)
async def set_vc(ctx, name):
    s = Session(); conf = s.query(ServerConfig).filter_by(guild_id=str(ctx.guild.id)).first() or ServerConfig(guild_id=str(ctx.guild.id)); s.add(conf)
    conf.target_vc_name = name; s.commit(); s.close(); await ctx.send(f"✅ 出席対象VCを「{name}」に設定しました。")

@bot.command()
@commands.has_permissions(administrator=True)
async def export_vc(ctx):
    s = Session(); h = s.query(VCState).filter_by(guild_id=str(ctx.guild.id)).all(); o = io.StringIO(); w = csv.writer(o)
    w.writerow(["名前", "VC名", "入室時間", "退出時間"])
    for x in h: w.writerow([x.user_name, x.channel_name, x.joined_at, x.left_at or "通話中"])
    o.seek(0); await ctx.send(file=discord.File(io.BytesIO(o.getvalue().encode()), filename="vc_log.csv")); s.close()

@bot.command()
async def sync(ctx):
    all_o = fetch_all_orgs()
    if await core_sync_logic(ctx.author, ctx.guild, all_o): await ctx.send("✅ 同期が完了しました。")

# --- UIパネル ---
class MultiFunctionView(discord.ui.View):
    def __init__(self): super().__init__(timeout=None)
    @discord.ui.button(label="一括同期", style=discord.ButtonStyle.danger, custom_id="sync_all")
    async def sync_all(self, interaction, button):
        if not interaction.user.guild_permissions.administrator: return
        await interaction.response.send_message("一括同期を開始...", ephemeral=True)
        all_o = fetch_all_orgs(); count = 0
        async for m in interaction.guild.fetch_members(limit=None):
            if await core_sync_logic(m, interaction.guild, all_o): count += 1
            await asyncio.sleep(0.1)
        await interaction.followup.send(f"同期完了: {count}名", ephemeral=True)

    @discord.ui.button(label="部長出席", style=discord.ButtonStyle.primary, custom_id="att_n")
    async def att_n(self, interaction, button): await self._log(interaction, "部長出席")

    @discord.ui.button(label="代理出席", style=discord.ButtonStyle.danger, custom_id="att_p")
    async def att_p(self, interaction, button): await self._log(interaction, "代理出席")

    async def _log(self, interaction, status):
        await interaction.response.defer(ephemeral=True); all_o = fetch_all_orgs(); dn = interaction.user.display_name; found = [o for o in all_o if o.org_name.lower() in dn.lower() or (o.alias and o.alias.lower() in dn.lower())]; org_name = found[0].org_name if found else "その他"; s = Session()
        try:
            s.add(AttendanceLog(guild_id=str(interaction.guild.id), user_id=str(interaction.user.id), user_name=dn, org_name=org_name, status=status)); s.commit()
            await interaction.followup.send(f"✅ {org_name} {status}を記録しました。", ephemeral=True)
        except: s.rollback()
        finally: s.close()

# --- Flask & 起動管理 ---
app = Flask(__name__)
@app.route('/')
def home(): return "OK", 200

def run_bot():
    while True:
        try: bot.run(os.environ.get("DISCORD_TOKEN"))
        except Exception as e:
            print(f"❌ Error: {e}"); time.sleep(30)

if __name__ == "__main__":
    threading.Thread(target=run_bot, daemon=True).start()
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000)))