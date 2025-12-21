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

# --- DB設定 (v5) ---
DATABASE_URL = os.environ.get("DATABASE_URL")
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
Session = sessionmaker(bind=engine)
Base = declarative_base()

class MasterOrg(Base):
    __tablename__ = 'master_org_v5'
    id = Column(Integer, primary_key=True, autoincrement=True)
    org_name = Column(String, unique=True, nullable=False)
    alias = Column(String, index=True)
    exclude_leader = Column(Boolean, default=False)

class AttendanceLog(Base):
    __tablename__ = 'attendance_log_v5'
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String)
    user_name = Column(String)
    org_name = Column(String)
    status = Column(String) # "通常", "代理"
    timestamp = Column(DateTime, default=lambda: datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))))

Base.metadata.create_all(engine)

# --- 定数 ---
CATEGORY_NAME = '団体用'
LEADER_ROLE_NAME = '部長'
PROXY_ROLE_NAME = '代理'

def fetch_org_data():
    session = Session()
    try:
        orgs = session.query(MasterOrg).all()
        mapping = {o.org_name.lower(): o for o in orgs}
        for o in orgs:
            if o.alias: mapping[o.alias.lower()] = o
        return orgs, mapping
    finally: session.close()

# --- UI Views ---
class MultiFunctionView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    # ロール同期
    @discord.ui.button(label="ロール・個室同期", style=discord.ButtonStyle.green, custom_id="sync_v5")
    async def sync(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        match = re.search(r'[@＠](.+)$', interaction.user.display_name)
        if not match: return await interaction.followup.send("⚠️ 名前を「名前@団体名」にしてください。")
        
        _, org_map = fetch_org_data()
        org_data = org_map.get(match.group(1).strip().lower())
        if not org_data: return await interaction.followup.send("🚫 団体未登録です。")

        guild = interaction.guild
        org_role = discord.utils.get(guild.roles, name=org_data.org_name) or await guild.create_role(name=org_data.org_name)
        leader_role = discord.utils.get(guild.roles, name=LEADER_ROLE_NAME) or await guild.create_role(name=LEADER_ROLE_NAME)
        proxy_role = discord.utils.get(guild.roles, name=PROXY_ROLE_NAME) or await guild.create_role(name=PROXY_ROLE_NAME)

        await interaction.user.add_roles(org_role)
        if "代理" in interaction.user.display_name:
            await interaction.user.add_roles(proxy_role)
            if leader_role in interaction.user.roles: await interaction.user.remove_roles(leader_role)
            msg = f"✅ {org_data.org_name} (代理) として同期しました。"
        else:
            if not org_data.exclude_leader: await interaction.user.add_roles(leader_role)
            if proxy_role in interaction.user.roles: await interaction.user.remove_roles(proxy_role)
            msg = f"✅ {org_data.org_name} (部長) として同期しました。"
        await interaction.followup.send(msg)

    # 出席ボタン
    @discord.ui.button(label="通常出席", style=discord.ButtonStyle.primary, custom_id="att_n_v5")
    async def att_n(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._log(interaction, "通常")

    @discord.ui.button(label="代理出席", style=discord.ButtonStyle.danger, custom_id="att_p_v5")
    async def att_p(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._log(interaction, "代理")

    # DB照合レポート
    @discord.ui.button(label="📊 団体出席照合レポート", style=discord.ButtonStyle.secondary, custom_id="report_v5")
    async def report(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message("❌ 管理者専用", ephemeral=True)
        await interaction.response.defer()
        
        all_orgs, org_map = fetch_org_data()
        present_org_names = set()
        vc_details = []

        # 現在VCにいる人をスキャン
        for member in interaction.guild.members:
            if member.voice:
                match = re.search(r'[@＠](.+)$', member.display_name)
                if match:
                    org_key = match.group(1).strip().lower()
                    if org_key in org_map:
                        found_org = org_map[org_key].org_name
                        present_org_names.add(found_org)
                        vc_details.append(f"🔵 {found_org} | {member.display_name}")

        all_org_names = {o.org_name for o in all_orgs}
        absent_orgs = all_org_names - present_org_names

        res = "**【📊 団体出席照合レポート】**\n\n"
        res += "**▼ VC参加中団体**\n" + ("\n".join(vc_details) if vc_details else "なし") + "\n\n"
        res += "**▼ 未参加（欠席）団体**\n" + ("\n".join([f"❌ {o}" for o in absent_orgs]) if absent_orgs else "全団体出席中！")
        
        await interaction.followup.send(res)

    async def _log(self, interaction, status):
        await interaction.response.defer(ephemeral=True)
        match = re.search(r'[@＠](.+)$', interaction.user.display_name)
        if not match: return await interaction.followup.send("⚠️ @団体名 が必要です。")
        _, org_map = fetch_org_data()
        org_data = org_map.get(match.group(1).strip().lower())
        if not org_data: return await interaction.followup.send("🚫 団体未登録です。")
        
        session = Session()
        session.add(AttendanceLog(user_id=str(interaction.user.id), user_name=interaction.user.display_name, org_name=org_data.org_name, status=status))
        session.commit(); session.close()
        await interaction.followup.send(f"✅ {org_data.org_name} ({status}) の出席を記録しました。")

# --- Bot構成 ---
bot = commands.Bot(command_prefix='!', intents=discord.Intents.all())

@bot.event
async def on_ready():
    bot.add_view(MultiFunctionView())
    print(f"✅ Bot Online: {bot.user}")

@bot.command()
@commands.has_permissions(administrator=True)
async def panel(ctx):
    await ctx.send("**【文化Bot 統合管理システム】**", view=MultiFunctionView())

@bot.command()
@commands.has_permissions(administrator=True)
async def add_org(ctx, name: str, alias: str = None, exclude: bool = False):
    session = Session()
    try:
        session.add(MasterOrg(org_name=name, alias=alias, exclude_leader=exclude))
        session.commit()
        await ctx.send(f"✅ 団体「{name}」をDBに登録しました。")
    except: await ctx.send("❌ 登録失敗（既に存在する可能性があります）")
    finally: session.close()

# --- Flask ---
app = Flask(__name__)
@app.route('/')
def h(): return "OK"
if __name__ == "__main__":
    threading.Thread(target=lambda: app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000))), daemon=True).start()
    bot.run(os.environ.get("DISCORD_TOKEN"))