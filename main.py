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

# --- Discord Bot 実装 ---
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.guilds = True

bot = commands.Bot(command_prefix='!', intents=intents)

# 【修正】コマンドを確実に実行させるためのイベント
@bot.event
async def on_message(message):
    if message.author == bot.user:
        return
    # コマンドを処理する
    await bot.process_commands(message)

# --- UI Views (ボタン処理) ---

class RoleCheckView(discord.ui.View):
    def __init__(self): super().__init__(timeout=None)
    @discord.ui.button(label="所属を同期する", style=discord.ButtonStyle.primary, custom_id="v_full_sync_v2")
    async def sync(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        display_name = interaction.user.display_name
        match = re.search(r'[@＠](.+)$', display_name)
        if not match: return await interaction.followup.send("⚠️ 名前を「@団体名」にしてください。")
        
        session = Session()
        config = session.query(ServerConfig).filter_by(guild_id=str(guild.id)).first()
        if not config:
            config = ServerConfig(guild_id=str(guild.id), guild_name=guild.name)
            session.add(config); session.commit(); session.refresh(config)
        
        org_key = match.group(1).strip().lower()
        org = session.query(OrgSettings).filter(
            OrgSettings.guild_id == str(guild.id),
            ((OrgSettings.org_name.ilike(org_key)) | (OrgSettings.alias.ilike(org_key)))
        ).first()
        session.close()

        if not org: return await interaction.followup.send(f"🚫 「{org_key}」は未登録です。")
        
        org_role = discord.utils.get(guild.roles, name=org.org_name) or await guild.create_role(name=org.org_name)
        await interaction.user.add_roles(org_role)
        
        is_proxy = "代理" in display_name
        leader_role = discord.utils.get(guild.roles, name=config.leader_role_name) or await guild.create_role(name=config.leader_role_name)
        
        if not org.exclude_leader and not is_proxy:
            await interaction.user.add_roles(leader_role)
            msg = f"✅ {org.org_name} 同期完了（部長付与）"
        else:
            if leader_role in interaction.user.roles: await interaction.user.remove_roles(leader_role)
            msg = f"✅ {org.org_name} 同期完了"
        
        cat = discord.utils.get(guild.categories, name=config.target_category)
        if cat:
            ch_name = org.org_name.lower().replace(" ", "-")
            if not any(ch_name in c.name.lower() for c in cat.text_channels):
                overwrites = {guild.default_role: discord.PermissionOverwrite(read_messages=False),
                              org_role: discord.PermissionOverwrite(read_messages=True),
                              guild.me: discord.PermissionOverwrite(read_messages=True)}
                await guild.create_text_channel(ch_name, category=cat, overwrites=overwrites)
        await interaction.followup.send(msg)

class AttendanceView(discord.ui.View):
    def __init__(self): super().__init__(timeout=None)
    @discord.ui.button(label="通常参加", style=discord.ButtonStyle.green, custom_id="v_full_reg_v2")
    async def reg(self, interaction, button): await self._proc(interaction, False)
    @discord.ui.button(label="代理参加", style=discord.ButtonStyle.red, custom_id="v_full_prx_v2")
    async def prx(self, interaction, button): await self._proc(interaction, True)
    
    async def _proc(self, interaction, is_proxy):
        await interaction.response.defer(ephemeral=True)
        match = re.search(r'[@＠](.+)$', interaction.user.display_name)
        if not match: return await interaction.followup.send("⚠️ 名前修正を！")
        
        session = Session()
        org = session.query(OrgSettings).filter(
            OrgSettings.guild_id == str(interaction.guild.id),
            ((OrgSettings.org_name.ilike(match.group(1).strip())) | (OrgSettings.alias.ilike(match.group(1).strip())))
        ).first()
        
        if not org: 
            session.close()
            return await interaction.followup.send("🚫 団体未登録")
            
        session.add(Attendance(guild_id=str(interaction.guild.id), user_id=str(interaction.user.id), org_name=org.org_name, is_proxy=is_proxy))
        session.commit(); session.close()
        await interaction.followup.send("✅ 出席を記録しました。")

# --- Bot 本体 ---

@bot.event
async def on_ready():
    bot.add_view(RoleCheckView()); bot.add_view(AttendanceView())
    print(f"✅ Bot Online: {bot.user}")

@bot.command()
@commands.has_permissions(administrator=True)
async def panel(ctx): await ctx.send("**所属確認パネル**", view=RoleCheckView())

@bot.command()
@commands.has_permissions(administrator=True)
async def attend_panel(ctx): await ctx.send("**出席記録パネル**", view=AttendanceView())

@bot.command()
@commands.has_permissions(administrator=True)
async def add_org(ctx, name: str, alias: str = None, exclude: bool = False):
    s = Session()
    s.add(OrgSettings(guild_id=str(ctx.guild.id), org_name=name, alias=alias, exclude_leader=exclude))
    s.commit(); s.close()
    await ctx.send(f"✅ {name} を登録しました。")

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
            s = Session(); s.query(ServerConfig).first(); s.close()
        except: pass
        time.sleep(300)

if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    threading.Thread(target=ping, daemon=True).start()
    bot.run(os.environ.get("DISCORD_TOKEN"))