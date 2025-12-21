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

# --- SQLAlchemy 完全再構築 ---
from sqlalchemy import create_engine, Column, String, Boolean, DateTime, Integer
from sqlalchemy.orm import sessionmaker, declarative_base

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    sys.exit(1)

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# 接続設定の最適化
engine = create_engine(DATABASE_URL, pool_pre_ping=True)
Session = sessionmaker(bind=engine)
Base = declarative_base()

# --- 新設計のテーブル (v3) ---
class MasterOrg(Base):
    """団体マスタ: 全サーバー共通の団体リスト"""
    __tablename__ = 'master_org_v3'
    id = Column(Integer, primary_key=True, autoincrement=True)
    org_name = Column(String, unique=True, nullable=False) # 団体名
    alias = Column(String, index=True)                     # 略称
    exclude_leader = Column(Boolean, default=False)       # 部長ロールを付与しない団体か

class AttendanceLog(Base):
    """出席ログ: 誰がいつ参加したかの記録"""
    __tablename__ = 'attendance_log_v3'
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String, nullable=False)
    user_name = Column(String)
    org_name = Column(String)
    is_proxy = Column(Boolean)
    timestamp = Column(DateTime, default=datetime.datetime.utcnow)

# テーブルをゼロから作成（存在しない場合のみ）
Base.metadata.create_all(engine)

# --- 定数設定 ---
CATEGORY_NAME = '団体用'
LEADER_ROLE_NAME = '部長'

# --- 共通関数 ---
def fetch_org_map():
    session = Session()
    try:
        orgs = session.query(MasterOrg).all()
        # 団体名と略称の両方から検索できるようにマッピング
        mapping = {o.org_name.lower(): o for o in orgs}
        for o in orgs:
            if o.alias:
                mapping[o.alias.lower()] = o
        return mapping
    finally:
        session.close()

# --- UI Views ---

class RoleSyncView(discord.ui.View):
    """所属確認パネルのボタン処理"""
    def __init__(self):
        super().__init__(timeout=None)
    
    @discord.ui.button(label="ロール・個室を同期", style=discord.ButtonStyle.green, custom_id="sync_v3")
    async def sync_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        user = interaction.user
        guild = interaction.guild
        
        # ニックネームから「@団体名」を抽出
        match = re.search(r'[@＠](.+)$', user.display_name)
        if not match:
            return await interaction.followup.send("⚠️ ニックネームを「名前@団体名」に変更してから押してください。", ephemeral=True)
        
        org_key = match.group(1).strip().lower()
        org_map = fetch_org_map()
        org_data = org_map.get(org_key)
        
        if not org_data:
            return await interaction.followup.send(f"🚫 「{org_key}」は団体リストに登録されていません。", ephemeral=True)

        # 1. 団体ロールの作成・付与
        target_org_name = org_data.org_name
        org_role = discord.utils.get(guild.roles, name=target_org_name) or await guild.create_role(name=target_org_name, mentionable=True)
        await user.add_roles(org_role)
        
        # 2. 部長ロールの判定 (代理がいなくて、かつ除外団体でなければ付与)
        is_proxy = "代理" in user.display_name
        leader_role = discord.utils.get(guild.roles, name=LEADER_ROLE_NAME) or await guild.create_role(name=LEADER_ROLE_NAME)
        
        if not org_data.exclude_leader and not is_proxy:
            await user.add_roles(leader_role)
            result_msg = f"✅ 「{target_org_name}」と「{LEADER_ROLE_NAME}」を付与しました。"
        else:
            if leader_role in user.roles:
                await user.remove_roles(leader_role)
            result_msg = f"✅ 「{target_org_name}」を付与しました（部長ロール対象外）。"

        # 3. チャンネルの自動作成
        category = discord.utils.get(guild.categories, name=CATEGORY_NAME)
        if category:
            ch_name = target_org_name.lower().replace(" ", "-")
            if not any(ch_name in c.name.lower() for c in category.text_channels):
                overwrites = {
                    guild.default_role: discord.PermissionOverwrite(read_messages=False),
                    org_role: discord.PermissionOverwrite(read_messages=True),
                    guild.me: discord.PermissionOverwrite(read_messages=True)
                }
                await guild.create_text_channel(ch_name, category=category, overwrites=overwrites)
        
        await interaction.followup.send(result_msg, ephemeral=True)

# --- Bot コマンド ---

intents = discord.Intents.all()
bot = commands.Bot(command_prefix='!', intents=intents)

@bot.event
async def on_ready():
    bot.add_view(RoleSyncView())
    print(f"✅ Bot Online: {bot.user}")

@bot.command()
@commands.has_permissions(administrator=True)
async def add_org(ctx, name: str, alias: str = None, exclude: bool = False):
    """!add_org 団体名 [略称] [True/False]"""
    session = Session()
    try:
        new_org = MasterOrg(org_name=name, alias=alias, exclude_leader=exclude)
        session.add(new_org)
        session.commit()
        await ctx.send(f"✅ 団体「{name}」をDBに新規登録しました。")
    except Exception as e:
        session.rollback()
        await ctx.send(f"❌ 登録失敗: その団体名は既に存在するか、DBエラーです。")
    finally:
        session.close()

@bot.command()
@commands.has_permissions(administrator=True)
async def panel(ctx):
    """認証パネルを設置"""
    await ctx.send(f"**【所属・個室同期パネル】**\n名前を「@団体名」に変えてから下のボタンを押してください。", view=RoleSyncView())

# --- インフラ維持 (Render用) ---
app = Flask(__name__)
@app.route('/')
def health(): return "Ready"

def run_flask():
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000)))

if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    bot.run(os.environ.get("DISCORD_TOKEN"))