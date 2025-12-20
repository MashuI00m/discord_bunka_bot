import os
import sys
import re 
from threading import Thread
from flask import Flask
import discord
from discord.ext import commands
import requests
import time
import asyncio
import datetime

# --- SQLAlchemy 設定 ---
from sqlalchemy import create_engine, Column, String, Boolean, DateTime
from sqlalchemy.orm import sessionmaker
from sqlalchemy.ext.declarative import declarative_base

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    print("FATAL: DATABASE_URL is not set.")
    sys.exit(1)

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
Session = sessionmaker(bind=engine)
Base = declarative_base()

class OrgSettings(Base):
    __tablename__ = 'org_settings'
    org_name = Column(String, primary_key=True)
    alias = Column(String, nullable=True)

class Attendance(Base):
    __tablename__ = 'attendance'
    user_id = Column(String, primary_key=True)
    org_name = Column(String)
    is_proxy = Column(Boolean)
    timestamp = Column(DateTime, default=datetime.datetime.utcnow)

Base.metadata.create_all(engine)

# --- 定数 ---
LOG_CHANNEL_NAME = '管理ログ'
PROXY_ROLE_NAME = 'Proxy Attendee' 
SHARED_CATEGORY_NAME = '会議室'

# --- 共通関数 ---
def get_allowed_orgs_map():
    session = Session()
    try:
        org_map = {}
        for org in session.query(OrgSettings).all():
            org_map[org.org_name.lower()] = org.org_name
            if org.alias: org_map[org.alias.lower()] = org.org_name
        return org_map
    finally: session.close()

def record_attendance(user_id, org_name, is_proxy):
    session = Session()
    try:
        record = session.query(Attendance).filter_by(user_id=user_id).first()
        if record:
            record.org_name, record.is_proxy = org_name, is_proxy
            record.timestamp = datetime.datetime.utcnow()
        else:
            session.add(Attendance(user_id=user_id, org_name=org_name, is_proxy=is_proxy))
        session.commit()
        return True
    except:
        session.rollback()
        return False
    finally: session.close()

async def sync_org_channel_permissions(guild, role, org_name):
    category = discord.utils.get(guild.categories, name=SHARED_CATEGORY_NAME)
    if not category: return
    search_name = org_name.lower().replace(" ", "-")
    target_channel = next((c for c in category.text_channels if search_name in c.name.lower()), None)
    overwrites = {guild.default_role: discord.PermissionOverwrite(read_messages=False),
                  role: discord.PermissionOverwrite(read_messages=True),
                  guild.me: discord.PermissionOverwrite(read_messages=True)}
    if target_channel: await target_channel.edit(overwrites=overwrites)
    else: await guild.create_text_channel(search_name, category=category, overwrites=overwrites)

# --- UI Views ---

class RoleCheckView(discord.ui.View):
    def __init__(self): super().__init__(timeout=None)
    @discord.ui.button(label="ロール・個室を自動取得", style=discord.ButtonStyle.green, custom_id="role_check_final")
    async def check_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True) # インタラクション失敗防止
        user, guild = interaction.user, interaction.guild
        org_map = get_allowed_orgs_map()
        match = re.search(r'[@＠](.+)$', user.display_name)
        if not match: return await interaction.followup.send("⚠️ 名前を「@団体名」にしてください。")
        org_key = match.group(1).strip().lower()
        role_name = org_map.get(org_key)
        if not role_name: return await interaction.followup.send(f"🚫 「{org_key}」は未登録です。")
        role = discord.utils.get(guild.roles, name=role_name) or await guild.create_role(name=role_name, mentionable=True)
        try:
            await user.add_roles(role)
            await sync_org_channel_permissions(guild, role, role_name)
            await interaction.followup.send(f"✅ 「{role_name}」を同期しました。")
        except: await interaction.followup.send("❌ 権限エラー。Botのロール順位を確認してください。")

class AttendanceView(discord.ui.View):
    def __init__(self): super().__init__(timeout=None)
    @discord.ui.button(label="通常参加", style=discord.ButtonStyle.green, custom_id="att_reg")
    async def reg(self, interaction, button): await self._process(interaction, False)
    @discord.ui.button(label="代理参加", style=discord.ButtonStyle.red, custom_id="att_prx")
    async def prx(self, interaction, button): await self._process(interaction, True)
    @discord.ui.button(label="🔊 通話中レポート", style=discord.ButtonStyle.blurple, custom_id="att_rpt")
    async def rpt(self, interaction, button):
        if not interaction.user.guild_permissions.administrator: return await interaction.response.send_message("❌ 管理者のみ", ephemeral=True)
        await interaction.response.defer()
        org_map = get_allowed_orgs_map()
        v_mems = [f"{org_map.get(re.search(r'[@＠](.+)$', m.display_name).group(1).strip().lower(), '不明') if re.search(r'[@＠](.+)$', m.display_name) else '不明'} | {m.display_name} | {m.voice.channel.name}" for m in interaction.guild.members if m.voice]
        await interaction.followup.send("🔊 **通話中**\n```\n" + ("\n".join(v_mems) if v_mems else "なし") + "\n```")

    async def _process(self, interaction, is_proxy):
        await interaction.response.defer(ephemeral=True)
        match = re.search(r'[@＠](.+)$', interaction.user.display_name)
        if not match: return await interaction.followup.send("⚠️ 名前を修正してください。")
        org_name = get_allowed_orgs_map().get(match.group(1).strip().lower())
        if not org_name: return await interaction.followup.send("🚫 団体未登録です。")
        record_attendance(str(interaction.user.id), org_name, is_proxy)
        await interaction.followup.send(f"✅ {'代理' if is_proxy else '通常'}で出席記録しました。")

# --- Bot 本体 ---
intents = discord.Intents.default()
intents.members = intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

@bot.event
async def on_ready():
    bot.add_view(RoleCheckView())
    bot.add_view(AttendanceView())
    print(f"Logged in as {bot.user}")

@bot.command()
@commands.has_permissions(administrator=True)
async def panel(ctx): await ctx.send("【所属確認】", view=RoleCheckView())

@bot.command()
@commands.has_permissions(administrator=True)
async def attend_panel(ctx): await ctx.send("【出席記録】", view=AttendanceView())

@bot.command()
@commands.has_permissions(administrator=True)
async def add_org(ctx, name: str, alias: str = None):
    session = Session()
    try:
        session.add(OrgSettings(org_name=name, alias=alias))
        session.commit()
        await ctx.send(f"✅ {name} を登録しました。")
    except: await ctx.send("❌ 失敗。")
    finally: session.close()

# --- 生存維持スレッド ---
app = Flask(__name__)
@app.route('/')
def h(): return "ok"
def k(): Thread(target=lambda: app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))).start()
    while True:
        try: requests.get(os.environ.get("RENDER_EXTERNAL_URL")); Session().query(OrgSettings).first()
        except: pass
        time.sleep(300)

if __name__ == "__main__":
    k()
    bot.run(os.environ.get("DISCORD_TOKEN"))