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

# --- SQLAlchemy 設定 (Supabase永続化) ---
from sqlalchemy import create_engine, Column, String, Boolean, DateTime, Integer
from sqlalchemy.orm import sessionmaker, declarative_base

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    print("FATAL: DATABASE_URL is not set.")
    sys.exit(1)

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
Session = sessionmaker(bind=engine)
Base = declarative_base()

# --- データベースモデル ---
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

# --- UI Views (ボタン処理) ---

class RoleCheckView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
    
    @discord.ui.button(label="所属を同期する", style=discord.ButtonStyle.primary, custom_id="v10_sync")
    async def sync(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        config = get_server_config(guild.id, guild.name)
        display_name = interaction.user.display_name
        
        match = re.search(r'[@＠](.+)$', display_name)
        if not match:
            return await interaction.followup.send("⚠️ 名前を「名前@団体名」にしてください。", ephemeral=True)
        
        org_key = match.group(1).strip().lower()
        org_map = get_allowed_orgs_map(guild.id)
        org = org_map.get(org_key)

        if not org:
            return await interaction.followup.send(f"🚫 「{org_key}」はこのサーバーに未登録です。", ephemeral=True)

        # 団体ロール付与
        org_role = discord.utils.get(guild.roles, name=org.org_name) or await guild.create_role(name=org.org_name)
        await interaction.user.add_roles(org_role)
        
        # 部長ロール判定 (代理が名前にあれば除外)
        is_proxy_user = "代理" in display_name
        leader_role = discord.utils.get(guild.roles, name=config.leader_role_name) or await guild.create_role(name=config.leader_role_name)
        
        if not org.exclude_leader and not is_proxy_user:
            await interaction.user.add_roles(leader_role)
            msg = f"✅ {org.org_name} ＆ {config.leader_role_name} として同期完了！"
        else:
            if leader_role in interaction.user.roles:
                await interaction.user.remove_roles(leader_role)
            msg = f"✅ {org.org_name} として同期完了（役職なし）"
        
        # チャンネル同期
        category = discord.utils.get(guild.categories, name=config.target_category)
        if category:
            chan_name = org.org_name.lower().replace(" ", "-")
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(read_messages=False),
                org_role: discord.PermissionOverwrite(read_messages=True),
                guild.me: discord.PermissionOverwrite(read_messages=True)
            }
            if not any(chan_name in c.name.lower() for c in category.text_channels):
                await guild.create_text_channel(chan_name, category=category, overwrites=overwrites)
        
        await interaction.followup.send(msg, ephemeral=True)

class AttendanceView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
    
    @discord.ui.button(label="通常参加", style=discord.ButtonStyle.green, custom_id="v10_reg")
    async def reg(self, interaction, button):
        await self._proc(interaction, False)
    
    @discord.ui.button(label="代理参加", style=discord.ButtonStyle.red, custom_id="v10_prx")
    async def prx(self, interaction, button):
        await self._proc(interaction, True)
    
    @discord.ui.button(label="🔊 通話中レポート", style=discord.ButtonStyle.blurple, custom_id="v10_vc")
    async def vc(self, interaction, button):
        if not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message("❌ 管理者のみ実行可能です。", ephemeral=True)
        await interaction.response.defer()
        org_map = get_allowed_orgs_map(interaction.guild.id)
        v_list = []
        for m in interaction.guild.members:
            if m.voice:
                match = re.search(r'[@＠](.+)$', m.display_name)
                org_obj = org_map.get(match.group(1).strip().lower()) if match else None
                org_name = org_obj.org_name if org_obj else "不明"
                v_list.append(f"{org_name} | {m.display_name} | {m.voice.channel.name}")
        await interaction.followup.send("🔊 **通話中一覧**\n```\n" + ("\n".join(v_list) if v_list else "誰もいません") + "\n```")

    async def _proc(self, interaction, is_proxy):
        await interaction.response.defer(ephemeral=True)
        match = re.search(r'[@＠](.+)$', interaction.user.display_name)
        if not match:
            return await interaction.followup.send("⚠️ 名前を「@団体名」にしてください。", ephemeral=True)
        org_map = get_allowed_orgs_map(interaction.guild.id)
        org = org_map.get(match.group(1).strip().lower())
        if not org:
            return await interaction.followup.send("🚫 団体未登録です。", ephemeral=True)
        
        session = Session()
        session.add(Attendance(guild_id=str(interaction.guild.id), user_id=str(interaction.user.id), org_name=org.org_name, is_proxy=is_proxy))
        session.commit()
        session.close()
        await interaction.followup.send(f"✅ {'代理' if is_proxy else '通常'}出席を記録しました。", ephemeral=True)

# --- Bot 本体 ---
intents = discord.Intents.default()
intents.members = True          #
intents.message_content = True  #
intents.guilds = True

bot = commands.Bot(command_prefix='!', intents=intents)

@bot.event
async def on_ready():
    # 起動時にViewを登録（永続ボタンの有効化）
    bot.add_view(RoleCheckView())
    bot.add_view(AttendanceView())
    print(f"✅ Logged in as {bot.user.name}")

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return
    await bot.process_commands(message) #

# --- 管理コマンド ---

@bot.command()
@commands.has_permissions(administrator=True)
async def panel(ctx):
    await ctx.send(f"**{ctx.guild.name} 認証パネル**\nボタンを押すとロールと個室を同期します。", view=RoleCheckView())

@bot.command()
@commands.has_permissions(administrator=True)
async def attend_panel(ctx):
    await ctx.send("**出席確認パネル**", view=AttendanceView())

@bot.command()
@commands.has_permissions(administrator=True)
async def add_org(ctx, name: str, alias: str = None, exclude_leader: bool = False):
    session = Session()
    session.add(OrgSettings(guild_id=str(ctx.guild.id), org_name=name, alias=alias, exclude_leader=exclude_leader))
    session.commit()
    session.close()
    await ctx.send(f"✅ {name} を登録しました。")

@bot.command()
@commands.has_permissions(administrator=True)
async def clear_attendance(ctx):
    session = Session()
    session.query(Attendance).filter_by(guild_id=str(ctx.guild.id)).delete()
    session.commit()
    session.close()
    await ctx.send("✅ 出席記録を全削除しました。")

# --- インフラ維持 (Flask) ---
app = Flask(__name__)
@app.route('/')
def h(): return "Bot is running!"

def run_flask():
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000)))

def keep_alive_ping():
    time.sleep(10)
    while True:
        try:
            url = os.environ.get("RENDER_EXTERNAL_URL")
            if url: requests.get(url)
            # DB生存確認
            session = Session()
            session.query(ServerConfig).first()
            session.close()
        except: pass
        time.sleep(300)

if __name__ == "__main__":
    Thread(target=run_flask).start()
    Thread(target=keep_alive_ping).start()
    bot.run(os.environ.get("DISCORD_TOKEN"))