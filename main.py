import os
import re 
import threading
import io
import csv
from flask import Flask
import discord
from discord.ext import commands
from sqlalchemy import create_engine, Column, String, Boolean, DateTime, Integer
from sqlalchemy.orm import sessionmaker, declarative_base
import datetime
import asyncio

# --- DB設定 (v16) ---
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

class AttendanceLog(Base):
    __tablename__ = 'attendance_log_v16'
    id = Column(Integer, primary_key=True, autoincrement=True)
    guild_id = Column(String)
    user_id = Column(String)
    user_name = Column(String)
    org_name = Column(String)
    status = Column(String)
    timestamp = Column(DateTime, default=lambda: datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))))

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

async def core_sync_logic(user, guild, all_orgs):
    if user.bot: return None
    dn = user.display_name
    found = [o for o in all_orgs if o.org_name.lower() in dn.lower() or (o.alias and o.alias.lower() in dn.lower())]
    conf = get_config(guild.id)
    log_ch = discord.utils.get(guild.text_channels, name=conf.admin_log_channel)

    if len(found) > 1:
        if log_ch: await log_ch.send(f"🚨 重複検知: {user.mention} (表示名: {dn}) をスキップしました。")
        return f"🚫 {dn}: 重複検知"
    if not found: return f"⚠️ {dn}: 団体なし"

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

    if target.skip_channel: return f"✅ {dn} 同期完了(個室なし)"

    cat = discord.utils.get(guild.categories, name=conf.category_name) or await guild.create_category(conf.category_name)
    ch_n = target.org_name.lower().replace(" ", "-")
    chan = next((c for c in cat.text_channels if ch_n in c.name.lower()), None)
    ow = {guild.default_role: discord.PermissionOverwrite(read_messages=False),
          o_role: discord.PermissionOverwrite(read_messages=True, send_messages=True),
          guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True)}
    if not chan: await guild.create_text_channel(ch_n, category=cat, overwrites=ow)
    else: await chan.edit(overwrites=ow)
    return f"✅ {dn} 同期完了"

bot = commands.Bot(command_prefix='!', intents=discord.Intents.all())

@bot.event
async def on_ready():
    bot.add_view(MultiFunctionView())
    print(f"✅ Online")
    for g in bot.guilds:
        c = get_config(g.id)
        ch = discord.utils.get(g.text_channels, name=c.admin_log_channel)
        if ch:
            async for m in ch.history(limit=10):
                if m.author == bot.user and "統合管理パネル" in m.content: await m.delete()
            await ch.send(f"**【{g.name} 統合管理パネル】**", view=MultiFunctionView())

@bot.event
async def on_voice_state_update(m, b, a):
    if m.bot: return
    c = get_config(m.guild.id)
    if not c.target_vc_name: return
    s = Session()
    try:
        now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
        if a.channel and a.channel.name == c.target_vc_name:
            s.query(VCState).filter_by(user_id=str(m.id), guild_id=str(m.guild.id), left_at=None).update({VCState.left_at: now})
            s.add(VCState(guild_id=str(m.guild.id), user_id=str(m.id), user_name=m.display_name, channel_name=a.channel.name))
        if b.channel and b.channel.name == c.target_vc_name:
            if not a.channel or a.channel.name != c.target_vc_name:
                rec = s.query(VCState).filter_by(user_id=str(m.id), guild_id=str(m.guild.id), left_at=None).first()
                if rec: rec.left_at = now
        s.commit()
    finally: s.close()

@bot.command()
@commands.has_permissions(administrator=True)
async def set_config(ctx, cat, leader, proxy, log):
    """【管理者】基本設定の更新"""
    s = Session()
    try:
        conf = s.query(ServerConfig).filter_by(guild_id=str(ctx.guild.id)).first()
        if not conf:
            conf = ServerConfig(guild_id=str(ctx.guild.id))
            s.add(conf)
        conf.category_name = cat
        conf.leader_role_name = leader
        conf.proxy_role_name = proxy
        conf.admin_log_channel = log
        s.commit()
        await ctx.send(f"✅ 設定を更新しました。\nカテゴリ: {cat}\n部長役職: {leader}\n代理役職: {proxy}\nログch: {log}")
    finally: s.close()

@bot.command()
@commands.has_permissions(administrator=True)
async def set_vc(ctx, vc_name):
    """【管理者】監視VCの設定"""
    s = Session()
    try:
        conf = s.query(ServerConfig).filter_by(guild_id=str(ctx.guild.id)).first()
        if not conf:
            conf = ServerConfig(guild_id=str(ctx.guild.id))
            s.add(conf)
        conf.target_vc_name = vc_name
        s.commit()
        await ctx.send(f"✅ 監視VCを「{vc_name}」に設定しました。")
    finally: s.close()

@bot.command()
@commands.has_permissions(administrator=True)
async def list_orgs(ctx):
    all_o = fetch_all_orgs()
    if not all_o: return await ctx.send("登録なし")
    m = "📋 **登録団体一覧**\n```\n団体名 | 略称 | 部長除外 | 個室なし\n----------------------------------\n"
    for o in all_o: m += f"{o.org_name} | {o.alias or '-'} | {o.exclude_leader} | {o.skip_channel}\n"
    await ctx.send(m + "```")

@bot.command()
@commands.has_permissions(administrator=True)
async def del_org(ctx, name: str):
    s = Session()
    try:
        target = s.query(MasterOrg).filter_by(org_name=name).first()
        if not target: return await ctx.send(f"⚠️ {name} なし")
        s.delete(target); s.commit()
        await ctx.send(f"✅ {name} 削除")
    finally: s.close()

@bot.command()
@commands.has_permissions(administrator=True)
async def add_orgs(ctx, *, data: str):
    s = Session()
    try:
        lines = data.strip().split('\n'); succ = []
        for l in lines:
            p = l.split()
            if not p: continue
            try:
                o = MasterOrg(org_name=p[0], alias=p[1] if len(p)>1 else None, exclude_leader=p[2].lower()=='true' if len(p)>2 else False, skip_channel=p[3].lower()=='true' if len(p)>3 else False)
                s.add(o); s.commit(); succ.append(p[0])
            except: s.rollback()
        await ctx.send(f"✅ 登録: {', '.join(succ)}")
    finally: s.close()

@bot.command()
@commands.has_permissions(administrator=True)
async def report(ctx):
    c = get_config(ctx.guild.id)
    if not c.target_vc_name: return await ctx.send("VC設定なし")
    s = Session()
    try:
        t = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).replace(hour=0, minute=0, second=0, microsecond=0)
        v = {u.user_id: u.user_name for u in s.query(VCState).filter(VCState.guild_id == str(ctx.guild.id), VCState.joined_at >= t).all()}
        p = {u.user_id: u.user_name for u in s.query(AttendanceLog).filter(AttendanceLog.guild_id == str(ctx.guild.id), AttendanceLog.timestamp >= t, AttendanceLog.status.in_(["通常", "代理"])).all()}
        await ctx.send(f"📊 照合\n⚠️ 未押下: {', '.join([n for i, n in v.items() if i not in p]) or 'なし'}\n❓ VCなし: {', '.join([n for i, n in p.items() if i not in v]) or 'なし'}")
    finally: s.close()

@bot.command()
@commands.has_permissions(administrator=True)
async def export_vc(ctx):
    s = Session()
    try:
        h = s.query(VCState).filter_by(guild_id=str(ctx.guild.id)).all()
        o = io.StringIO(); w = csv.writer(o)
        w.writerow(["ユーザー", "VC", "入室", "退出"])
        for x in h: w.writerow([x.user_name, x.channel_name, x.joined_at, x.left_at or "中"])
        o.seek(0); await ctx.send(file=discord.File(io.BytesIO(o.getvalue().encode()), filename="vc.csv"))
    finally: s.close()

@bot.command()
async def sync(ctx):
    all_o = fetch_all_orgs()
    res = await core_sync_logic(ctx.author, ctx.guild, all_o)
    if res: await ctx.send(res)

@bot.command()
@commands.has_permissions(administrator=True)
async def panel(ctx):
    """【管理者】手動でパネルを投稿"""
    await ctx.send(f"**【{ctx.guild.name} 統合管理パネル】**", view=MultiFunctionView())

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

    @discord.ui.button(label="通常", style=discord.ButtonStyle.primary, custom_id="att_n")
    async def att_n(self, interaction, button): await self._log(interaction, "通常")
    @discord.ui.button(label="代理", style=discord.ButtonStyle.danger, custom_id="att_p")
    async def att_p(self, interaction, button): await self._log(interaction, "代理")
    @discord.ui.button(label="終了", style=discord.ButtonStyle.secondary, custom_id="att_e")
    async def att_e(self, interaction, button): await self._log(interaction, "終了")

    async def _log(self, interaction, status):
        await interaction.response.defer(ephemeral=True)
        all_o = fetch_all_orgs()
        f = [o for o in all_o if o.org_name.lower() in interaction.user.display_name.lower() or (o.alias and o.alias.lower() in interaction.user.display_name.lower())]
        if len(f) != 1: return await interaction.followup.send("不備あり")
        s = Session()
        try:
            s.add(AttendanceLog(guild_id=str(interaction.guild.id), user_id=str(interaction.user.id), user_name=interaction.user.display_name, org_name=f[0].org_name, status=status))
            s.commit()
        finally: s.close()
        await interaction.followup.send(f"✅ {f[0].org_name} {status}")

app = Flask(__name__)
@app.route('/')
def home(): return "OK", 200
def run_flask():
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000)))

if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    bot.run(os.environ.get("DISCORD_TOKEN"))