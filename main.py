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

engine = create_engine(
    DATABASE_URL, 
    pool_pre_ping=True, 
    pool_recycle=300,
    connect_args={'sslmode':'require'}
)
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

# --- 共通関数 ---
def get_config(guild_id):
    session = Session()
    try:
        conf = session.query(ServerConfig).filter_by(guild_id=str(guild_id)).first()
        if not conf:
            conf = ServerConfig(guild_id=str(guild_id))
            session.add(conf)
            session.commit()
            session.refresh(conf)
        return conf
    finally:
        session.close()

def fetch_all_orgs():
    session = Session()
    try:
        return session.query(MasterOrg).all()
    finally:
        session.close()

# --- 同期ロジック ---
async def core_sync_logic(user, guild, all_orgs):
    if user.bot: return None
    
    display_name = user.display_name
    found_orgs = []
    
    for org in all_orgs:
        is_matched = False
        if org.org_name.lower() in display_name.lower():
            is_matched = True
        elif org.alias and org.alias.lower() in display_name.lower():
            is_matched = True
        if is_matched:
            found_orgs.append(org)

    conf = get_config(guild.id)
    log_ch = discord.utils.get(guild.text_channels, name=conf.admin_log_channel)

    if len(found_orgs) > 1:
        org_names_str = ", ".join([o.org_name for o in found_orgs])
        if log_ch:
            await log_ch.send(f"🚨 **同期拒否（重複検知）**: {user.mention}\n表示名: `{display_name}`\n検知団体: `{org_names_str}`")
        return f"🚫 {display_name}: 重複検知のため同期を拒否しました"

    if not found_orgs:
        return f"⚠️ {display_name}: 団体名が見つかりません"

    target_org = found_orgs[0]
    skip_keywords = ["なし", "None", "none", "ナシ"]
    l_role_name = None if conf.leader_role_name in skip_keywords else conf.leader_role_name
    p_role_name = None if conf.proxy_role_name in skip_keywords else conf.proxy_role_name

    all_org_names = [o.org_name for o in all_orgs]
    cleanup_list = all_org_names.copy()
    if l_role_name: cleanup_list.append(l_role_name)
    if p_role_name: cleanup_list.append(p_role_name)
    
    roles_to_remove = [r for r in user.roles if r.name in cleanup_list and r.name != target_org.org_name]
    if roles_to_remove:
        await user.remove_roles(*roles_to_remove)

    o_role = discord.utils.get(guild.roles, name=target_org.org_name) or await guild.create_role(name=target_org.org_name, mentionable=True)
    if o_role not in user.roles:
        await user.add_roles(o_role)

    if p_role_name and (p_role_name in display_name or "代理" in display_name):
        p_role = discord.utils.get(guild.roles, name=p_role_name) or await guild.create_role(name=p_role_name)
        await user.add_roles(p_role)
    elif l_role_name and not target_org.exclude_leader:
        l_role = discord.utils.get(guild.roles, name=l_role_name) or await guild.create_role(name=l_role_name)
        await user.add_roles(l_role)

    if target_org.skip_channel:
        return f"✅ {display_name} 同期完了 (個室なし)"

    cat = discord.utils.get(guild.categories, name=conf.category_name) or await guild.create_category(conf.category_name)
    ch_name = target_org.org_name.lower().replace(" ", "-")
    chan = next((c for c in cat.text_channels if ch_name in c.name.lower()), None)
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(read_messages=False),
        o_role: discord.PermissionOverwrite(read_messages=True, send_messages=True),
        guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True)
    }
    if not chan:
        await guild.create_text_channel(ch_name, category=cat, overwrites=overwrites)
    else:
        await chan.edit(overwrites=overwrites)
    return f"✅ {display_name} 同期完了"

# --- Bot 本体 ---
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
                if m.author == bot.user and "統合管理パネル" in m.content:
                    await m.delete()
            await channel.send(f"**【{guild.name} 統合管理パネル】**\n（監視VC: {conf.target_vc_name or '未設定'}）", view=MultiFunctionView())

@bot.event
async def on_voice_state_update(member, before, after):
    if member.bot: return
    conf = get_config(member.guild.id)
    if not conf.target_vc_name: return
    s = Session()
    try:
        now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
        if after.channel and after.channel.name == conf.target_vc_name:
            s.query(VCState).filter_by(user_id=str(member.id), guild_id=str(member.guild.id), left_at=None).update({VCState.left_at: now})
            s.add(VCState(guild_id=str(member.guild.id), user_id=str(member.id), user_name=member.display_name, channel_name=after.channel.name))
        if before.channel and before.channel.name == conf.target_vc_name:
            if not after.channel or after.channel.name != conf.target_vc_name:
                record = s.query(VCState).filter_by(user_id=str(member.id), guild_id=str(member.guild.id), left_at=None).first()
                if record:
                    record.left_at = now
        s.commit()
    finally:
        s.close()

@bot.command()
@commands.has_permissions(administrator=True)
async def add_orgs(ctx, *, data: str):
    s = Session()
    try:
        lines = data.strip().split('\n')
        success = []
        for line in lines:
            parts = line.split()
            if not parts: continue
            try:
                org = MasterOrg(
                    org_name=parts[0], 
                    alias=parts[1] if len(parts)>1 else None, 
                    exclude_leader=parts[2].lower()=='true' if len(parts)>2 else False,
                    skip_channel=parts[3].lower()=='true' if len(parts)>3 else False
                )
                s.add(org)
                s.commit()
                success.append(parts[0])
            except:
                s.rollback()
        await ctx.send(f"✅ 登録完了: {', '.join(success)}")
    finally:
        s.close()

@bot.command()
@commands.has_permissions(administrator=True)
async def report(ctx):
    conf = get_config(ctx.guild.id)
    if not conf.target_vc_name: return await ctx.send("⚠️ VC設定がありません。")
    s = Session()
    try:
        today = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).replace(hour=0, minute=0, second=0, microsecond=0)
        v_hist = s.query(VCState).filter(VCState.guild_id == str(ctx.guild.id), VCState.joined_at >= today).all()
        pushed = s.query(AttendanceLog).filter(AttendanceLog.guild_id == str(ctx.guild.id), AttendanceLog.timestamp >= today, AttendanceLog.status.in_(["通常", "代理"])).all()
        v_ids = {u.user_id: u.user_name for u in v_hist}
        p_ids = {u.user_id: u.user_name for u in pushed}
        msg = f"📊 **本日の照合**\n⚠️ 未押下: {', '.join([n for i, n in v_ids.items() if i not in p_ids]) or 'なし'}\n❓ VC履歴なし: {', '.join([n for i, n in p_ids.items() if i not in v_ids]) or 'なし'}"
        await ctx.send(msg)
    finally:
        s.close()

@bot.command()
@commands.has_permissions(administrator=True)
async def export_vc(ctx):
    s = Session()
    try:
        hist = s.query(VCState).filter_by(guild_id=str(ctx.guild.id)).all()
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["ユーザー", "チャンネル", "入室", "退出"])
        for h in hist:
            writer.writerow([h.user_name, h.channel_name, h.joined_at, h.left_at or "入室中"])
        output.seek(0)
        await ctx.send(file=discord.File(io.BytesIO(output.getvalue().encode()), filename="vc_history.csv"))
    finally:
        s.close()

@bot.command()
async def sync(ctx):
    all_orgs = fetch_all_orgs()
    res = await core_sync_logic(ctx.author, ctx.guild, all_orgs)
    if res: await ctx.send(res)

class MultiFunctionView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
    
    @discord.ui.button(label="一括同期", style=discord.ButtonStyle.danger, custom_id="sync_all")
    async def sync_all(self, interaction, button):
        if not interaction.user.guild_permissions.administrator: return
        await interaction.response.send_message("🔄 同期中...", ephemeral=True)
        all_orgs = fetch_all_orgs()
        count = 0
        async for m in interaction.guild.fetch_members(limit=None):
            res = await core_sync_logic(m, interaction.guild, all_orgs)
            if res and "✅" in res: count += 1
        await interaction.followup.send(f"📊 完了: {count}名 (重複者はスキップ)")

    @discord.ui.button(label="通常", style=discord.ButtonStyle.primary, custom_id="att_n")
    async def att_n(self, interaction, button): await self._log(interaction, "通常")
    @discord.ui.button(label="代理", style=discord.ButtonStyle.danger, custom_id="att_p")
    async def att_p(self, interaction, button): await self._log(interaction, "代理")
    @discord.ui.button(label="終了", style=discord.ButtonStyle.secondary, custom_id="att_e")
    async def att_e(self, interaction, button): await self._log(interaction, "終了")

    async def _log(self, interaction, status):
        await interaction.response.defer(ephemeral=True)
        all_orgs = fetch_all_orgs()
        found = [o for o in all_orgs if o.org_name.lower() in interaction.user.display_name.lower() or (o.alias and o.alias.lower() in interaction.user.display_name.lower())]
        if len(found) != 1:
            return await interaction.followup.send("🚫 識別失敗または重複あり。")
        oname = found[0].org_name
        s = Session()
        try:
            s.add(AttendanceLog(guild_id=str(interaction.guild.id), user_id=str(interaction.user.id), user_name=interaction.user.display_name, org_name=oname, status=status))
            s.commit()
        finally:
            s.close()
        await interaction.followup.send(f"✅ {oname} {status}記録完了")

# --- Flask Server ---
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is running!", 200

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)

if __name__ == "__main__":
    t = threading.Thread(target=run_flask, daemon=True)
    t.start()
    bot.run(os.environ.get("DISCORD_TOKEN"))