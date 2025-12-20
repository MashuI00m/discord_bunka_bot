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
if DATABASE_URL is None:
    print("FATAL ERROR: DATABASE_URL が設定されていません。")
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

# --- Flask & 生存確認スレッド ---
app = Flask(__name__) 

@app.route('/')
def home():
    return "Bot is alive!"

def ping_self():
    url = os.environ.get("RENDER_EXTERNAL_URL")
    if not url: return
    while True:
        try:
            requests.get(url)
        except: pass
        time.sleep(300)

def keep_db_alive():
    """外部DBの停止を防ぐために定期アクセス"""
    while True:
        try:
            session = Session()
            session.query(OrgSettings).first()
            session.close()
            print("DB Ping: OK")
        except Exception as e:
            print(f"DB Ping Error: {e}")
        time.sleep(3600)

def keep_alive():
    Thread(target=lambda: app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))).start()
    Thread(target=ping_self).start()
    Thread(target=keep_db_alive).start()

# --- DB操作関数 ---

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

# --- チャンネル・権限ロジック ---

async def sync_org_channel_permissions(guild, role, org_name):
    """
    1. カテゴリ内に既存の合致するチャンネルがないか探す
    2. あれば権限を上書き追加、なければ新規作成
    """
    category = discord.utils.get(guild.categories, name=SHARED_CATEGORY_NAME)
    if not category:
        print(f"ERROR: カテゴリ '{SHARED_CATEGORY_NAME}' が見つかりません。")
        return

    # 団体名に基づいた検索用文字列 (小文字・ハイフン置換)
    search_name = org_name.lower().replace(" ", "-")
    
    # カテゴリ内のチャンネルをスキャン
    target_channel = None
    for channel in category.text_channels:
        # 名前が完全一致、もしくは団体名を含んでいる場合に合致とみなす
        if search_name in channel.name.lower():
            target_channel = channel
            break

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(read_messages=False),
        role: discord.PermissionOverwrite(read_messages=True),
        guild.me: discord.PermissionOverwrite(read_messages=True)
    }

    if target_channel:
        # 既存チャンネルの権限を更新
        try:
            await target_channel.edit(overwrites=overwrites)
            print(f"SUCCESS: 既存チャンネル '{target_channel.name}' の権限を更新しました。")
        except Exception as e:
            print(f"ERROR: 既存チャンネルの更新に失敗: {e}")
    else:
        # 新規作成
        try:
            await guild.create_text_channel(search_name, category=category, overwrites=overwrites)
            print(f"SUCCESS: 新規チャンネル '{search_name}' を作成しました。")
        except Exception as e:
            print(f"ERROR: チャンネルの新規作成に失敗: {e}")

# --- Discord Bot ロジック ---

intents = discord.Intents.default()
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

class RoleCheckView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="ロール・個室を自動取得", style=discord.ButtonStyle.green, custom_id="check_role")
    async def check_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        user = interaction.user
        guild = interaction.guild
        
        org_map = get_allowed_orgs_map()
        match = re.search(r'[@＠](.+)$', user.display_name)
        
        if not match:
            return await interaction.followup.send("⚠️ ニックネームを「名前@団体名」にしてください。", ephemeral=True)
        
        org_key = match.group(1).strip().lower()
        role_name = org_map.get(org_key)
        
        if not role_name:
            return await interaction.followup.send(f"🚫 団体名「{org_key}」は未登録です。", ephemeral=True)

        # ロール取得または作成
        role = discord.utils.get(guild.roles, name=role_name)
        if not role:
            try:
                role = await guild.create_role(name=role_name, mentionable=True)
            except:
                return await interaction.followup.send("❌ ロール作成権限がありません。", ephemeral=True)

        await user.add_roles(role)
        
        # 【重要】チャンネルの同期（既存チェック含む）
        await sync_org_channel_permissions(guild, role, role_name)
        
        await interaction.followup.send(f"✅ 「{role_name}」のロール付与とチャンネル確認が完了しました。", ephemeral=True)

# (出席確認パネルや他のコマンドは以前と同じため省略... 必要に応じて統合)

@bot.event
async def on_ready():
    bot.add_view(RoleCheckView())
    print(f'{bot.user} 起動完了')

@bot.command()
@commands.has_permissions(administrator=True)
async def panel(ctx):
    embed = discord.Embed(title="所属確認", description="ボタンを押すとロール付与と個室へのアクセス権を同期します。", color=discord.Color.blue())
    await ctx.send(embed=embed, view=RoleCheckView())

@bot.command()
@commands.has_permissions(administrator=True)
async def add_org(ctx, name: str, alias: str = None):
    session = Session()
    try:
        session.add(OrgSettings(org_name=name, alias=alias))
        session.commit()
        await ctx.send(f"✅ {name} を登録しました。")
    except: await ctx.send("❌ 登録失敗。")
    finally: session.close()

keep_alive()
bot.run(os.environ.get("DISCORD_TOKEN"))