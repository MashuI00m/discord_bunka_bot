import os
import sys
import re 
import threading
from flask import Flask
import discord
from discord.ext import commands
import requests
import time
import datetime

# --- SQLAlchemy 設定 ---
from sqlalchemy import create_engine, Column, String, Boolean, DateTime, Integer
from sqlalchemy.orm import sessionmaker, declarative_base

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    sys.exit(1)

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
Session = sessionmaker(bind=engine)
Base = declarative_base()

class OrgSettings(Base):
    __tablename__ = 'org_settings'
    id = Column(Integer, primary_key=True, autoincrement=True)
    org_name = Column(String)
    alias = Column(String, nullable=True)
    exclude_leader = Column(Boolean, default=False)

class Attendance(Base):
    __tablename__ = 'attendance'
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String)
    org_name = Column(String)
    is_proxy = Column(Boolean)
    timestamp = Column(DateTime, default=datetime.datetime.utcnow)

Base.metadata.create_all(engine)

# --- 定数 ---
SHARED_CATEGORY_NAME = '団体用'
LEADER_ROLE_NAME = '部長'

# --- 共通関数 ---
def get_allowed_orgs_map():
    session = Session()
    try:
        org_map = {}
        for org in session.query(OrgSettings).all():
            org_map[org.org_name.lower()] = org
            if org.alias: org_map[org.alias.lower()] = org
        return org_map
    finally: session.close()

# --- UI Views ---

class RoleCheckView(discord.ui.View):
    def __init__(self): super().__init__(timeout=None)
    @discord.ui.button(label="ロール・個室を自動取得", style=discord.ButtonStyle.green, custom_id="role_check_v12")
    async def sync(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        user, guild = interaction.user, interaction.guild
        org_map = get_allowed_orgs_map()
        
        match = re.search(r'[@＠](.+)$', user.display_name)
        if not match: return await interaction.followup.send("⚠️ 名前を「名前@団体名」にしてください。")
        
        org_key = match.group(1).strip().lower()
        org_data = org_map.get(org_key)
        if not org_data: return await interaction.followup.send(f"🚫 「{org_key}」は未登録です。")
        
        # 1. 団体ロール付与
        role_name = org_data.org_name
        role = discord.utils.get(guild.roles, name=role_name) or await guild.create_role(name=role_name, mentionable=True)
        await user.add_roles(role)
        
        # 2. 部長判定
        is_proxy = "代理" in user.display_name
        leader_role = discord.utils.get(guild.roles, name=LEADER_ROLE_NAME) or await guild.create_role(name=LEADER_ROLE_NAME)
        
        if not org_data.exclude_leader and not is_proxy:
            await user.add_roles(leader_role)
            leader_msg = f" ＆ {LEADER_ROLE_NAME}"
        else:
            if leader_role in user.roles: await user.remove_roles(leader_role)
            leader_msg = ""

        # 3. チャンネル作成
        category = discord.utils.get(guild.categories, name=SHARED_CATEGORY_NAME)
        if category:
            ch_name = role_name.lower().replace(" ", "-")
            overwrites = {guild.default_role: discord.PermissionOverwrite(read_messages=False),
                          role: discord.PermissionOverwrite(read_messages=True),
                          guild.me: discord.PermissionOverwrite(read_messages=True)}
            if not any(ch_name in c.name.lower() for c in category.text_channels):
                await guild.create_text_channel(ch_name, category=category, overwrites=overwrites)
        
        await interaction.followup.send(f"✅ 「{role_name}{leader_msg}」を同期完了！")

class AttendanceView(discord.ui.View):
    def __init__(self): super().__init__(timeout=None)
    @discord.ui.button(label="通常参加", style=discord.ButtonStyle.green, custom_id="att_reg_v12")
    async def reg(self, interaction, button): await self._process(interaction, False)
    @discord.ui.button(label="代理参加", style=discord.ButtonStyle.red, custom_id="att_prx_v12")
    async def prx(self, interaction, button): await self._process(interaction, True)
    @discord.ui.button(label="🔊 通話中レポート", style=discord.ButtonStyle.blurple, custom_id="att_vc_v12")
    async def vc(self, interaction, button):
        if not interaction.user.guild_permissions.administrator: return await interaction.response.send_message("❌ 管理者のみ", ephemeral=True)
        await interaction.response.defer()
        org_map = get_allowed_orgs_map()
        v_list = [f"{org_map.get(re.search(r'[@＠](.+)$', m.display_name).group(1).strip().lower(), type('O',(),{'org_name':'不明'})).org_name if re.search(r'[@＠](.+)$', m.display_name) else '不明'} | {m.display_name} | {m.voice.channel.name}" for m in interaction.guild.members if m.voice]
        await interaction.followup.send("🔊 通話中:\n```\n" + ("\n".join(v_list) if v_list else "なし") + "\n```")

    async def _process(self, interaction, is_proxy):
        await interaction.response.defer(ephemeral=True)
        match = re.search(r'[@＠](.+)$', interaction.user.display_name)
        if not match: return await interaction.followup.send("⚠️ 名前修正をしてください。")
        org_data = get_allowed_orgs_map().get(match.group(1).strip().lower())
        if not org_data: return await interaction.followup.send("🚫 未登録です。")
        session = Session()
        session.add(Attendance(user_id=str(interaction.user.id), org_name=org_data.org_name, is_proxy=is_proxy))
        session.commit(); session.close()
        await interaction.followup.send("✅ 出席を記録しました。")

# --- Bot 本体 ---
intents = discord.Intents.default()
intents.members = intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

@bot.event
async def on_ready():
    bot.add_view(RoleCheckView())
    bot.add_view(AttendanceView())
    print(f"✅ Logged in as {bot.user}")

@bot.command()
@commands.has_permissions(administrator=True)
async def panel(ctx): await ctx.send("【所属確認】", view=RoleCheckView())

@bot.command()
@commands.has_permissions(administrator=True)
async def attend_panel(ctx): await ctx.send("【出席記録】", view=AttendanceView())

@bot.command()
@commands.has_permissions(administrator=True)
async def add_org(ctx, name: str, alias: str = None, exclude: bool = False):
    session = Session()
    try:
        session.add(OrgSettings(org_name=name, alias=alias, exclude_leader=exclude))
        session.commit()
        await ctx.send(f"✅ {name} を登録しました。")
    except: await ctx.send("❌ 登録失敗。")
    finally: session.close()

# --- インフラ維持 ---
app = Flask(__name__)
@app.route('/')
def h(): return "ok"
def run_flask(): app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000)))
def ping():
    time.sleep(20)
    while True:
        try:
            url = os.environ.get("RENDER_EXTERNAL_URL")
            if url: requests.get(url)
            s = Session(); s.query(OrgSettings).first(); s.close()
        except: pass
        time.sleep(300)

if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    threading.Thread(target=ping, daemon=True).start()
    bot.run(os.environ.get("DISCORD_TOKEN"))