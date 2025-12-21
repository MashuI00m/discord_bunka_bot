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

# --- DB設定 ---
DATABASE_URL = os.environ.get("DATABASE_URL")
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
Session = sessionmaker(bind=engine)
Base = declarative_base()

class MasterOrg(Base):
    __tablename__ = 'master_org_v6'
    id = Column(Integer, primary_key=True, autoincrement=True)
    org_name = Column(String, unique=True, nullable=False)
    alias = Column(String, index=True)
    exclude_leader = Column(Boolean, default=False)

class AttendanceLog(Base):
    __tablename__ = 'attendance_log_v6'
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String)
    org_name = Column(String)
    status = Column(String)
    timestamp = Column(DateTime, default=lambda: datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))))

Base.metadata.create_all(engine)

# --- 定数 ---
CATEGORY_NAME = '団体用'
LEADER_ROLE_NAME = '部長'
PROXY_ROLE_NAME = '代理'

def fetch_all_orgs():
    session = Session()
    try:
        orgs = session.query(MasterOrg).all()
        return orgs
    finally: session.close()

# --- UI Views ---
class MultiFunctionView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="ロール・個室同期", style=discord.ButtonStyle.green, custom_id="sync_v6")
    async def sync(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        user, guild = interaction.user, interaction.guild
        
        # 1. ニックネーム判定
        match = re.search(r'[@＠](.+)$', user.display_name)
        if not match: return await interaction.followup.send("⚠️ 名前を「名前@団体名」にしてください。")
        
        org_key = match.group(1).strip().lower()
        all_orgs = fetch_all_orgs()
        org_map = {o.org_name.lower(): o for o in all_orgs}
        for o in all_orgs:
            if o.alias: org_map[o.alias.lower()] = o
            
        target_org = org_map.get(org_key)
        if not target_org: return await interaction.followup.send(f"🚫 「{org_key}」は未登録です。")

        # 2. 他団体のロールを削除（お掃除機能）
        all_org_names = [o.org_name for o in all_orgs]
        roles_to_remove = [r for r in user.roles if r.name in all_org_names and r.name != target_org.org_name]
        if roles_to_remove:
            await user.remove_roles(*roles_to_remove)

        # 3. 役職・団体ロールの付与
        org_role = discord.utils.get(guild.roles, name=target_org.org_name) or await guild.create_role(name=target_org.org_name, mentionable=True)
        leader_role = discord.utils.get(guild.roles, name=LEADER_ROLE_NAME) or await guild.create_role(name=LEADER_ROLE_NAME)
        proxy_role = discord.utils.get(guild.roles, name=PROXY_ROLE_NAME) or await guild.create_role(name=PROXY_ROLE_NAME)

        await user.add_roles(org_role)
        if "代理" in user.display_name:
            await user.add_roles(proxy_role)
            if leader_role in user.roles: await user.remove_roles(leader_role)
            status_msg = "（代理）"
        else:
            if not target_org.exclude_leader: await user.add_roles(leader_role)
            if proxy_role in user.roles: await user.remove_roles(proxy_role)
            status_msg = "（部長）"

        # 4. 個別部屋の作成・権限設定
        cat = discord.utils.get(guild.categories, name=CATEGORY_NAME) or await guild.create_category(CATEGORY_NAME)
        ch_name = target_org.org_name.lower().replace(" ", "-")
        target_channel = next((c for c in cat.text_channels if ch_name in c.name.lower()), None)
        
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            org_role: discord.PermissionOverwrite(read_messages=True, send_messages=True),
            guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True)
        }
        
        if not target_channel:
            await guild.create_text_channel(ch_name, category=cat, overwrites=overwrites)
            chan_msg = "＆ 個室を作成しました。"
        else:
            await target_channel.edit(overwrites=overwrites)
            chan_msg = "＆ 個室を同期しました。"

        await interaction.followup.send(f"✅ {target_org.org_name}{status_msg} {chan_msg}")

    @discord.ui.button(label="通常出席", style=discord.ButtonStyle.primary, custom_id="att_n_v6")
    async def att_n(self, interaction, button): await self._log(interaction, "通常")

    @discord.ui.button(label="代理出席", style=discord.ButtonStyle.danger, custom_id="att_p_v6")
    async def att_p(self, interaction, button): await self._log(interaction, "代理")

    @discord.ui.button(label="📊 団体出席照合レポート", style=discord.ButtonStyle.secondary, custom_id="rpt_v6")
    async def rpt(self, interaction, button):
        if not interaction.user.guild_permissions.administrator: return await interaction.response.send_message("❌ 管理者専用", ephemeral=True)
        await interaction.response.defer()
        all_orgs = fetch_all_orgs()
        org_map = {o.org_name.lower(): o.org_name for o in all_orgs}
        for o in all_orgs:
            if o.alias: org_map[o.alias.lower()] = o.org_name
        
        present = set()
        details = []
        for m in interaction.guild.members:
            if m.voice:
                match = re.search(r'[@＠](.+)$', m.display_name)
                if match:
                    oname = org_map.get(match.group(1).strip().lower())
                    if oname:
                        present.add(oname)
                        details.append(f"🔵 {oname} | {m.display_name}")
        
        absent = [o.org_name for o in all_orgs if o.org_name not in present]
        res = f"**【📊 出席照合】**\n参加: {len(present)} 団体 / 未着: {len(absent)} 団体\n\n"
        res += "**▼ VC参加中**\n" + ("\n".join(details) if details else "なし") + "\n\n"
        res += "**▼ 未参加（欠席）**\n" + ("\n".join([f"❌ {o}" for o in absent]) if absent else "全団体出席中！")
        await interaction.followup.send(res)

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
        await interaction.followup.send(f"✅ {oname} ({status}) 記録完了。")

# --- Bot ---
bot = commands.Bot(command_prefix='!', intents=discord.Intents.all())

@bot.event
async def on_ready():
    bot.add_view(MultiFunctionView())
    print(f"✅ Bot Online: {bot.user}")

@bot.command()
@commands.has_permissions(administrator=True)
async def panel(ctx): await ctx.send("**【管理システム】**", view=MultiFunctionView())

@bot.command()
@commands.has_permissions(administrator=True)
async def add_org(ctx, name: str, alias: str = None, exclude: bool = False):
    s = Session()
    try:
        s.add(MasterOrg(org_name=name, alias=alias, exclude_leader=exclude))
        s.commit()
        await ctx.send(f"✅ {name} 登録完了。")
    except: await ctx.send("❌ 重複またはエラー。")
    finally: s.close()

# --- Flask ---
app = Flask(__name__)
@app.route('/')
def h(): return "OK"
if __name__ == "__main__":
    threading.Thread(target=lambda: app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000))), daemon=True).start()
    bot.run(os.environ.get("DISCORD_TOKEN"))