import os
import sys
import re 
from threading import Thread
from flask import Flask
import discord
from discord.ext import commands
import requests
import time
import datetime

# --- SQLAlchemy 設定 ---
from sqlalchemy import create_engine, Column, String, Boolean, DateTime, Integer
from sqlalchemy.orm import sessionmaker, declarative_base # 警告が出ない書き方に修正

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    sys.exit(1)
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
Session = sessionmaker(bind=engine)
Base = declarative_base()

# テーブル定義
class ServerConfig(Base):
    __tablename__ = 'server_config'
    guild_id = Column(String, primary_key=True)
    guild_name = Column(String)
    target_category = Column(String, default="団体用")
    leader_role_name = Column(String, default="部長") 

class OrgSettings(Base):
    __tablename__ = 'org_settings'
    id = Column(Integer, primary_key=True, autoincrement=True)
    guild_id = Column(String)
    org_name = Column(String)
    alias = Column(String, nullable=True)
    exclude_leader = Column(Boolean, default=False)

class Attendance(Base):
    __tablename__ = 'attendance'
    id = Column(Integer, primary_key=True, autoincrement=True)
    guild_id = Column(String)
    user_id = Column(String)
    org_name = Column(String)
    is_proxy = Column(Boolean)
    timestamp = Column(DateTime, default=datetime.datetime.utcnow)

Base.metadata.create_all(engine)

# --- 共通関数 ---
def get_server_config(guild_id, guild_name):
    session = Session()
    config = session.query(ServerConfig).filter_by(guild_id=str(guild_id)).first()
    if not config:
        config = ServerConfig(guild_id=str(guild_id), guild_name=guild_name)
        session.add(config)
        session.commit()
        session.refresh(config)
    session.close()
    return config

def get_allowed_orgs_map(guild_id):
    session = Session()
    orgs = session.query(OrgSettings).filter_by(guild_id=str(guild_id)).all()
    org_map = {o.org_name.lower(): o for o in orgs}
    for o in orgs:
        if o.alias: org_map[o.alias.lower()] = o
    session.close()
    return org_map

# --- UI Views ---
class RoleCheckView(discord.ui.View):
    def __init__(self): super().__init__(timeout=None)
    @discord.ui.button(label="所属を同期する", style=discord.ButtonStyle.primary, custom_id="v9_sync")
    async def sync(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        config = get_server_config(guild.id, guild.name)
        display_name = interaction.user.display_name
        match = re.search(r'[@＠](.+)$', display_name)
        if not match: return await interaction.followup.send("⚠️ 名前を「@団体名」にしてください。")
        org_key = match.group(1).strip().lower()
        org_map = get_allowed_orgs_map(guild.id)
        org = org_map.get(org_key)
        if not org: return await interaction.followup.send(f"🚫 「{org_key}」は未登録です。")
        org_role = discord.utils.get(guild.roles, name=org.org_name) or await guild.create_role(name=org.org_name)
        await interaction.user.add_roles(org_role)
        is_proxy_user = "代理" in display_name
        leader_role = discord.utils.get(guild.roles, name=config.leader_role_name) or await guild.create_role(name=config.leader_role_name)
        if not org.exclude_leader and not is_proxy_user:
            await interaction.user.add_roles(leader_role)
            leader_msg = f" ＆ 「{config.leader_role_name}」"
        else:
            if leader_role in interaction.user.roles: await interaction.user.remove_roles(leader_role)
            leader_msg = "（代理/除外のため役職なし）"
        category = discord.utils.get(guild.categories, name=config.target_category)
        if category:
            chan_name = org.org_name.lower().replace(" ", "-")
            overwrites = {guild.default_role: discord.PermissionOverwrite(read_messages=False),
                          org_role: discord.PermissionOverwrite(read_messages=True),
                          guild.me: discord.PermissionOverwrite(read_messages=True)}
            if not any(chan_name in c.name.lower() for c in category.text_channels):
                await guild.create_text_channel(chan_name, category=category, overwrites=overwrites)
        await interaction.followup.send(f"✅ {org.org_name}{leader_msg} 同期完了")

class AttendanceView(discord.ui.View):
    def __init__(self): super().__init__(timeout=None)
    @discord.ui.button(label="通常参加", style=discord.ButtonStyle.green, custom_id="v9_reg")
    async def reg(self, interaction, button): await self._proc(interaction, False)
    @discord.ui.button(label="代理参加", style=discord.ButtonStyle.red, custom_id="v9_prx")
    async def prx(self, interaction, button): await self._proc(interaction, True)
    @discord.ui.button(label="🔊 通話中レポート", style=discord.ButtonStyle.blurple, custom_id="v9_vc")
    async def vc(self, interaction, button):
        if not interaction.user.guild_permissions.administrator: return await interaction.response.send_message("管理者専用", ephemeral=True)
        await interaction.response.defer()
        org_map = get_allowed_orgs_map(interaction.guild.id)
        v_list = [f"{org_map.get(re.search(r'[@＠](.+)$', m.display_name).group(1).strip().lower(), type('O',(),{'org_name':'不明'})).org_name if re.search(r'[@＠](.+)$', m.display_name) else '不明'} | {m.display_name} | {m.voice.channel.name}" for m in interaction.guild.members if m.voice]
        await interaction.followup.send("🔊 通話中:\n```\n" + ("\n".join(v_list) if v_list else "なし") + "\n```")
    async def _proc(self, interaction, is_proxy):
        await interaction.response.defer(ephemeral=True)
        match = re.search(r'[@＠](.+)$', interaction.user.display_name)
        if not match: return await interaction.followup.send("⚠️ 名前修正を！")
        org_map = get_allowed_orgs_map(interaction.guild.id)
        org = org_map.get(match.group(1).strip().lower())
        if not org: return await interaction.followup.send("🚫 団体未登録")
        session = Session()
        session.add(Attendance(guild_id=str(interaction.guild.id), user_id=str(interaction.user.id), org_name=org.org_name, is_proxy=is_proxy))
        session.commit(); session.close()
        await interaction.followup.send("✅ 記録完了")

# --- Bot 本体 ---
intents = discord.Intents.default()
intents.members = intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

@bot.event
async def on_ready():
    bot.add_view(RoleCheckView()); bot.add_view(AttendanceView())
    print(f"Logged in as {bot.user}")

@bot.command()
@commands.has_permissions(administrator=True)
async def panel(ctx): await ctx.send("**所属確認パネル**", view=RoleCheckView())

@bot.command()
@commands.has_permissions(administrator=True)
async def attend_panel(ctx): await ctx.send("**出席確認パネル**", view=AttendanceView())

@bot.command()
@commands.has_permissions(administrator=True)
async def add_org(ctx, name: str, alias: str = None, exclude_leader: bool = False):
    session = Session()
    session.add(OrgSettings(guild_id=str(ctx.guild.id), org_name=name, alias=alias, exclude_leader=exclude_leader))
    session.commit(); session.close()
    await ctx.send(f"✅ {name} を登録。")

# --- Flask & 起動 ---
app = Flask(__name__)
@app.route('/')
def h(): return "ok"

def run_flask():
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000)))

def keep_alive_ping():
    time.sleep(10)
    while True:
        try:
            url = os.environ.get("RENDER_EXTERNAL_URL")
            if url: requests.get(url)
            session = Session(); session.query(ServerConfig).first(); session.close()
        except: pass
        time.sleep(300)

if __name__ == "__main__":
    Thread(target=run_flask).start()
    Thread(target=keep_alive_ping).start()
    bot.run(os.environ.get("DISCORD_TOKEN"))