import os
import re 
import threading
from flask import Flask
import discord
from discord.ext import commands
from sqlalchemy import create_engine, Column, String, Boolean, DateTime, Integer
from sqlalchemy.orm import sessionmaker, declarative_base
import datetime
import asyncio

# --- DB設定 (v14) ---
DATABASE_URL = os.environ.get("DATABASE_URL")
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
Session = sessionmaker(bind=engine)
Base = declarative_base()

class MasterOrg(Base):
    __tablename__ = 'master_org_v14'
    id = Column(Integer, primary_key=True, autoincrement=True)
    org_name = Column(String, unique=True, nullable=False)
    alias = Column(String, index=True)
    exclude_leader = Column(Boolean, default=False)

class ServerConfig(Base):
    __tablename__ = 'server_config_v14'
    guild_id = Column(String, primary_key=True)
    category_name = Column(String, default="団体用")
    leader_role_name = Column(String, default="部長")
    proxy_role_name = Column(String, default="代理")
    admin_log_channel = Column(String, default="管理ログ")
    target_vc_name = Column(String, default=None)

class AttendanceLog(Base):
    __tablename__ = 'attendance_log_v14'
    id = Column(Integer, primary_key=True, autoincrement=True)
    guild_id = Column(String)
    user_id = Column(String)
    user_name = Column(String)
    org_name = Column(String)
    status = Column(String)
    timestamp = Column(DateTime, default=lambda: datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))))

class VCState(Base):
    __tablename__ = 'vc_state_v14'
    id = Column(Integer, primary_key=True, autoincrement=True)
    guild_id = Column(String)
    user_id = Column(String)
    user_name = Column(String)
    channel_name = Column(String)
    joined_at = Column(DateTime, default=lambda: datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))))

Base.metadata.create_all(engine)

# --- 共通関数 ---
def get_config(guild_id):
    session = Session(); conf = session.query(ServerConfig).filter_by(guild_id=str(guild_id)).first()
    if not conf: conf = ServerConfig(guild_id=str(guild_id)); session.add(conf); session.commit(); session.refresh(conf)
    session.close(); return conf

def fetch_all_orgs():
    session = Session(); try: return session.query(MasterOrg).all()
    finally: session.close()

# --- 同期コアロジック ---
async def core_sync_logic(user, guild, all_orgs):
    if user.bot: return None
    match = re.search(r'[@＠](.+)$', user.display_name)
    if not match: return f"⚠️ {user.display_name}: 形式不備"
    
    org_key = match.group(1).strip().lower()
    org_map = {o.org_name.lower(): o for o in all_orgs}
    for o in all_orgs:
        if o.alias: org_map[o.alias.lower()] = o
    
    target_org = org_map.get(org_key)
    if not target_org: return f"🚫 {user.display_name}: 「{org_key}」未登録"

    conf = get_config(guild.id)
    skip_keywords = ["なし", "None", "none", "ナシ"]
    l_role_name = None if conf.leader_role_name in skip_keywords else conf.leader_role_name
    p_role_name = None if conf.proxy_role_name in skip_keywords else conf.proxy_role_name

    all_org_names = [o.org_name for o in all_orgs]
    cleanup_list = all_org_names.copy()
    if l_role_name: cleanup_list.append(l_role_name)
    if p_role_name: cleanup_list.append(p_role_name)
    
    roles_to_remove = [r for r in user.roles if r.name in cleanup_list and r.name != target_org.org_name]
    if roles_to_remove: await user.remove_roles(*roles_to_remove)

    o_role = discord.utils.get(guild.roles, name=target_org.org_name) or await guild.create_role(name=target_org.org_name, mentionable=True)
    await user.add_roles(o_role)

    if p_role_name and (p_role_name in user.display_name or "代理" in user.display_name):
        p_role = discord.utils.get(guild.roles, name=p_role_name) or await guild.create_role(name=p_role_name)
        await user.add_roles(p_role)
    elif l_role_name and not target_org.exclude_leader:
        l_role = discord.utils.get(guild.roles, name=l_role_name) or await guild.create_role(name=l_role_name)
        await user.add_roles(l_role)

    cat = discord.utils.get(guild.categories, name=conf.category_name) or await guild.create_category(conf.category_name)
    ch_name = target_org.org_name.lower().replace(" ", "-")
    chan = next((c for c in cat.text_channels if ch_name in c.name.lower()), None)
    overwrites = {guild.default_role: discord.PermissionOverwrite(read_messages=False),
                  o_role: discord.PermissionOverwrite(read_messages=True, send_messages=True),
                  guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True)}
    if not chan: await guild.create_text_channel(ch_name, category=cat, overwrites=overwrites)
    else: await chan.edit(overwrites=overwrites)
    return f"✅ {user.display_name} 同期完了"

# --- UI ---
class MultiFunctionView(discord.ui.View):
    def __init__(self): super().__init__(timeout=None)

    @discord.ui.button(label="全員一括同期", style=discord.ButtonStyle.danger, custom_id="sync_all_v14")
    async def sync_all(self, interaction, button):
        if not interaction.user.guild_permissions.administrator: return
        await interaction.response.send_message("🔄 一括同期を開始...", ephemeral=True)
        all_orgs = fetch_all_orgs(); count = 0
        async for m in interaction.guild.fetch_members(limit=None):
            res = await core_sync_logic(m, interaction.guild, all_orgs)
            if res and "✅" in res: count += 1
            await asyncio.sleep(0.4)
        await interaction.followup.send(f"📊 同期完了: {count}名")

    @discord.ui.button(label="通常出席", style=discord.ButtonStyle.primary, custom_id="att_n_v14")
    async def att_n(self, interaction, button): await self._log(interaction, "通常")
    @discord.ui.button(label="代理出席", style=discord.ButtonStyle.danger, custom_id="att_p_v14")
    async def att_p(self, interaction, button): await self._log(interaction, "代理")
    @discord.ui.button(label="終了", style=discord.ButtonStyle.secondary, custom_id="att_e_v14")
    async def att_e(self, interaction, button): await self._log(interaction, "終了")

    async def _log(self, interaction, status):
        await interaction.response.defer(ephemeral=True)
        match = re.search(r'[@＠](.+)$', interaction.user.display_name)
        if not match: return await interaction.followup.send("⚠️ @団体名 が必要です。")
        all_orgs = fetch_all_orgs(); oname = next((o.org_name for o in all_orgs if o.org_name.lower() == match.group(1).strip().lower() or (o.alias and o.alias.lower() == match.group(1).strip().lower())), None)
        if not oname: return await interaction.followup.send("🚫 未登録です。")
        s = Session(); s.add(AttendanceLog(guild_id=str(interaction.guild.id), user_id=str(interaction.user.id), user_name=interaction.user.display_name, org_name=oname, status=status)); s.commit(); s.close()
        await interaction.followup.send(f"✅ {oname} 【{status}】を記録。")

# --- Bot ---
bot = commands.Bot(command_prefix='!', intents=discord.Intents.all())

@bot.event
async def on_ready():
    bot.add_view(MultiFunctionView())
    print(f"✅ Bot Online: {bot.user}")
    for guild in bot.guilds:
        conf = get_config(guild.id)
        channel = discord.utils.get(guild.text_channels, name=conf.admin_log_channel)
        if channel:
            async for m in channel.history(limit=10):
                if m.author == bot.user and "統合管理パネル" in m.content: await m.delete()
            await channel.send(f"**【{guild.name} 統合管理パネル】**\n（監視VC: {conf.target_vc_name or '未設定'}）", view=MultiFunctionView())

@bot.event
async def on_voice_state_update(member, before, after):
    if member.bot: return
    conf = get_config(member.guild.id)
    if not conf.target_vc_name: return
    s = Session()
    if after.channel and after.channel.name == conf.target_vc_name:
        s.add(VCState(guild_id=str(member.guild.id), user_id=str(member.id), user_name=member.display_name, channel_name=after.channel.name))
    if before.channel and before.channel.name == conf.target_vc_name:
        if not after.channel or after.channel.name != conf.target_vc_name:
            s.query(VCState).filter_by(user_id=str(member.id), guild_id=str(member.guild.id)).delete()
    s.commit(); s.close()

@bot.command()
@commands.has_permissions(administrator=True)
async def set_config(ctx, category: str = "団体用", leader: str = "部長", proxy: str = "代理", log_channel: str = "管理ログ"):
    s = Session(); conf = s.query(ServerConfig).filter_by(guild_id=str(ctx.guild.id)).first()
    if not conf: conf = ServerConfig(guild_id=str(ctx.guild.id))
    conf.category_name, conf.leader_role_name, conf.proxy_role_name, conf.admin_log_channel = category, leader, proxy, log_channel
    s.add(conf); s.commit(); s.close(); await ctx.send("✅ 基本設定を更新しました。")

@bot.command()
@commands.has_permissions(administrator=True)
async def set_vc(ctx, name: str):
    s = Session(); conf = s.query(ServerConfig).filter_by(guild_id=str(ctx.guild.id)).first()
    if not conf: conf = ServerConfig(guild_id=str(ctx.guild.id))
    conf.target_vc_name = name; s.add(conf); s.commit(); s.close(); await ctx.send(f"✅ 監視VCを「{name}」に設定。")

@bot.command()
@commands.has_permissions(administrator=True)
async def report(ctx):
    conf = get_config(ctx.guild.id)
    if not conf.target_vc_name: return await ctx.send("⚠️ `!set_vc` で監視VCを指定してください。")
    s = Session(); vc_users = s.query(VCState).filter_by(guild_id=str(ctx.guild.id)).all()
    limit = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))) - datetime.timedelta(hours=2)
    pushed = s.query(AttendanceLog).filter(AttendanceLog.guild_id == str(ctx.guild.id), AttendanceLog.timestamp >= limit, AttendanceLog.status.in_(["通常", "代理"])).all()
    s.close(); p_ids = {u.user_id for u in pushed}; v_ids = {u.user_id for u in vc_users}
    msg = f"📊 **出欠照合** ({conf.target_vc_name})\n---\n⚠️ **VCにいるが未押下**:\n"
    msg += (', '.join([u.user_name for u in vc_users if u.user_id not in p_ids]) or "なし") + "\n\n❓ **押下したがVC不在**:\n"
    msg += (', '.join(list({u.user_name for u in pushed if u.user_id not in v_ids})) or "なし")
    await ctx.send(msg)

@bot.command()
@commands.has_permissions(administrator=True)
async def add_orgs(ctx, *, data: str):
    s = Session(); lines = data.strip().split('\n'); success, error = [], []
    for line in lines:
        parts = line.split(); name = parts[0]; alias = parts[1] if len(parts) > 1 else None
        exclude = parts[2].lower() == 'true' if len(parts) > 2 else False
        try: s.add(MasterOrg(org_name=name, alias=alias, exclude_leader=exclude)); s.commit(); success.append(name)
        except: s.rollback(); error.append(name)
    s.close(); await ctx.send(f"✅ 登録成功: {', '.join(success)}")

@bot.command()
@commands.has_permissions(administrator=True)
async def panel(ctx):
    await ctx.send(f"**【{ctx.guild.name} 統合管理パネル】**", view=MultiFunctionView())

# --- Flask ---
app = Flask(__name__); @app.route('/')
def h(): return "OK"
if __name__ == "__main__":
    threading.Thread(target=lambda: app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000))), daemon=True).start()
    bot.run(os.environ.get("DISCORD_TOKEN"))