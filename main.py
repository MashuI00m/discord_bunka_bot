import os
import sys
import re 
import threading
from flask import Flask
import discord
from discord.ext import commands
from sqlalchemy import create_engine, Column, String, Boolean, DateTime, Integer
from sqlalchemy.orm import sessionmaker, declarative_base
import datetime

# --- DB設定 (v8に刷新) ---
DATABASE_URL = os.environ.get("DATABASE_URL")
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
Session = sessionmaker(bind=engine)
Base = declarative_base()

class MasterOrg(Base):
    __tablename__ = 'master_org_v8'
    id = Column(Integer, primary_key=True, autoincrement=True)
    org_name = Column(String, unique=True, nullable=False)
    alias = Column(String, index=True)
    exclude_leader = Column(Boolean, default=False)

class AttendanceLog(Base):
    __tablename__ = 'attendance_log_v8'
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String)
    org_name = Column(String)
    status = Column(String) # "通常", "代理", "終了"
    timestamp = Column(DateTime, default=lambda: datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))))

Base.metadata.create_all(engine)

# --- 定数 ---
CATEGORY_NAME = '団体用'
LEADER_ROLE_NAME = '部長'
PROXY_ROLE_NAME = '代理'

def fetch_all_orgs():
    session = Session()
    try: return session.query(MasterOrg).all()
    finally: session.close()

# --- 共通処理: 同期ロジックコア ---
async def core_sync_logic(user, guild):
    match = re.search(r'[@＠](.+)$', user.display_name)
    if not match: return "⚠️ 名前を「名前@団体名」にしてください。"
    
    org_key = match.group(1).strip().lower()
    all_orgs = fetch_all_orgs()
    org_map = {o.org_name.lower(): o for o in all_orgs}
    for o in all_orgs:
        if o.alias: org_map[o.alias.lower()] = o
        
    target_org = org_map.get(org_key)
    if not target_org: return f"🚫 「{org_key}」は未登録です。"

    # ロール掃除 (全団体 + 部長 + 代理)
    all_org_names = [o.org_name for o in all_orgs]
    cleanup_list = all_org_names + [LEADER_ROLE_NAME, PROXY_ROLE_NAME]
    roles_to_remove = [r for r in user.roles if r.name in cleanup_list and r.name != target_org.org_name]
    
    if roles_to_remove: await user.remove_roles(*roles_to_remove)

    # ロール付与
    org_role = discord.utils.get(guild.roles, name=target_org.org_name) or await guild.create_role(name=target_org.org_name, mentionable=True)
    leader_role = discord.utils.get(guild.roles, name=LEADER_ROLE_NAME) or await guild.create_role(name=LEADER_ROLE_NAME)
    proxy_role = discord.utils.get(guild.roles, name=PROXY_ROLE_NAME) or await guild.create_role(name=PROXY_ROLE_NAME)

    await user.add_roles(org_role)
    if "代理" in user.display_name:
        await user.add_roles(proxy_role)
    elif not target_org.exclude_leader:
        await user.add_roles(leader_role)

    # 部屋作成/同期
    cat = discord.utils.get(guild.categories, name=CATEGORY_NAME) or await guild.create_category(CATEGORY_NAME)
    ch_name = target_org.org_name.lower().replace(" ", "-")
    target_channel = next((c for c in cat.text_channels if ch_name in c.name.lower()), None)
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(read_messages=False),
        org_role: discord.PermissionOverwrite(read_messages=True, send_messages=True),
        guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True)
    }
    if not target_channel: await guild.create_text_channel(ch_name, category=cat, overwrites=overwrites)
    else: await target_channel.edit(overwrites=overwrites)
    
    return f"✅ {target_org.org_name} の同期を完了しました。"

# --- UI Views ---
class MultiFunctionView(discord.ui.View):
    def __init__(self): super().__init__(timeout=None)

    @discord.ui.button(label="ロール・個室同期", style=discord.ButtonStyle.green, custom_id="sync_v8")
    async def sync_btn(self, interaction: discord.Interaction, button):
        await interaction.response.defer(ephemeral=True)
        res = await core_sync_logic(interaction.user, interaction.guild)
        await interaction.followup.send(res)

    @discord.ui.button(label="通常出席", style=discord.ButtonStyle.primary, custom_id="att_n_v8")
    async def att_n(self, interaction, button): await self._log(interaction, "通常")

    @discord.ui.button(label="代理出席", style=discord.ButtonStyle.danger, custom_id="att_p_v8")
    async def att_p(self, interaction, button): await self._log(interaction, "代理")

    @discord.ui.button(label="終了", style=discord.ButtonStyle.secondary, custom_id="att_e_v8")
    async def att_e(self, interaction, button): await self._log(interaction, "終了")

    async def _log(self, interaction, status):
        await interaction.response.defer(ephemeral=True)
        match = re.search(r'[@＠](.+)$', interaction.user.display_name)
        if not match: return await interaction.followup.send("⚠️ @団体名 が必要です。")
        all_orgs = fetch_all_orgs()
        org_map = {o.org_name.lower(): o.org_name for o in all_orgs}
        for o in all_orgs:
            if o.alias: org_map[o.alias.lower()] = o.org_name
        oname = org_map.get(match.group(1).strip().lower())
        if not oname: return await interaction.followup.send("🚫 未登録です。")
        s = Session(); s.add(AttendanceLog(user_id=str(interaction.user.id), org_name=oname, status=status))
        s.commit(); s.close()
        await interaction.followup.send(f"✅ {oname} の【{status}】を記録しました。")

# --- Bot Commands ---
bot = commands.Bot(command_prefix='!', intents=discord.Intents.all())

@bot.event
async def on_ready():
    bot.add_view(MultiFunctionView())
    print(f"✅ Bot Online: {bot.user}")

@bot.command()
async def sync(ctx):
    """個別コマンド: !sync で即座に同期"""
    res = await core_sync_logic(ctx.author, ctx.guild)
    await ctx.send(res)

@bot.command()
@commands.has_permissions(administrator=True)
async def panel(ctx):
    await ctx.send("**【総合パネル】**", view=MultiFunctionView())

@bot.command()
@commands.has_permissions(administrator=True)
async def report(ctx):
    all_orgs = fetch_all_orgs()
    org_map = {o.org_name.lower(): o.org_name for o in all_orgs}
    for o in all_orgs:
        if o.alias: org_map[o.alias.lower()] = o.org_name
    present = set(); details = []
    for m in ctx.guild.members:
        if m.voice:
            match = re.search(r'[@＠](.+)$', m.display_name)
            oname = org_map.get(match.group(1).strip().lower()) if match else None
            if oname: present.add(oname); details.append(f"🔵 {oname} | {m.display_name}")
    absent = [o.org_name for o in all_orgs if o.org_name not in present]
    res = f"**【📊 出席照合】**\n参加: {len(present)} / 未着: {len(absent)}\n\n"
    res += "**▼ VC参加中**\n" + ("\n".join(details) if details else "なし") + "\n\n"
    res += "**▼ 未参加**\n" + ("\n".join([f"❌ {o}" for o in absent]) if absent else "全団体出席中！")
    await ctx.send(res)

@bot.command()
@commands.has_permissions(administrator=True)
async def add_org(ctx, name: str, alias: str = None, exclude: bool = False):
    s = Session()
    try:
        s.add(MasterOrg(org_name=name, alias=alias, exclude_leader=exclude))
        s.commit(); await ctx.send(f"✅ {name} を登録しました。")
    except: await ctx.send("❌ エラー。既に存在する可能性があります。")
    finally: s.close()

# --- Flask ---
app = Flask(__name__)
@app.route('/')
def h(): return "OK"
if __name__ == "__main__":
    threading.Thread(target=lambda: app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000))), daemon=True).start()
    bot.run(os.environ.get("DISCORD_TOKEN"))