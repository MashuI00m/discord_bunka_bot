import os
import sys
import re 
from threading import Thread
from flask import Flask
import discord
from discord.ext import commands

# --- DB/SQLAlchemy 関連のインポート ---
from sqlalchemy import create_engine, Column, String, Integer
from sqlalchemy.orm import sessionmaker
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.exc import OperationalError, SQLAlchemyError 
# ------------------------------------

# --- DB接続設定 ---
DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    print("WARNING: DATABASE_URL 環境変数が設定されていません。DB機能は無効です。", file=sys.stderr)

# データベースエンジンの初期化 (Bot起動時に一度だけ実行される)
engine = None
Session = None
Base = declarative_base()

if DATABASE_URL:
    try:
        engine = create_engine(DATABASE_URL)
        Session = sessionmaker(bind=engine)
        print("データベース接続エンジンを初期化しました。")
    except Exception as e:
        print(f"FATAL ERROR: DB接続エンジンの初期化に失敗しました: {e}", file=sys.stderr)
        # 接続失敗時もBotは起動させるため、sys.exit(1)はコメントアウト

# --- DBテーブル定義 ---
class OrgSettings(Base):
    """許可された団体名を保存するテーブル"""
    __tablename__ = 'allowed_organizations'
    
    id = Column(Integer, primary_key=True)
    org_name = Column(String, unique=True, nullable=False)

    def __repr__(self):
        return f"<OrgSettings(org_name='{self.org_name}')>"

# --- Flask Webサーバーの設定 ---
app = Flask(__name__) # '__name__' に修正 ('main' ではなく標準的な記述)

# ルートURL（"/"）にアクセスがあったときに実行される関数
@app.route('/')
def home():
    return "Bot is alive!"

def run_server():
    port = int(os.environ.get("PORT", 5000))
    # gunicornがサーバー起動を担うため、app.run()は不要またはコメントアウトが推奨されます
    # app.run(host='0.0.0.0', port=port)
    pass 

def keep_alive():
    t = Thread(target=run_server)
    t.start()

# --- Bot設定 ---
LOG_CHANNEL_NAME = '管理ログ'

DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
if DISCORD_TOKEN is None:
    print("FATAL ERROR: DISCORD_TOKEN 環境変数が設定されていません。", file=sys.stderr)
    sys.exit(1)

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix='!', intents=intents)
# 許可された団体名リストはDBから取得するロジックに変更するため、ここは一時的に不要
# allowed_orgs = set() 

# --- DB操作関数 ---

def get_allowed_orgs():
    """DBから許可された団体名のリスト（セット）を取得する"""
    if not Session:
        return set()
    session = Session()
    try:
        # DBに保存されているすべての団体名を取得し、セットに変換
        orgs = session.query(OrgSettings.org_name).all()
        return set(o[0] for o in orgs)
    except OperationalError as e:
        # テーブルが存在しない、または接続エラーの場合
        print(f"DB Operational Error: {e}")
        return set()
    except SQLAlchemyError as e:
        print(f"DB Error during fetching organizations: {e}")
        return set()
    finally:
        session.close()

# ---------------------------------------------------------
# ボタンの定義 (View)
# ---------------------------------------------------------
class RoleCheckView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="ロール・個室を自動取得", style=discord.ButtonStyle.green, custom_id="check_role_button")
    async def check_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)

        user = interaction.user
        guild = interaction.guild
        display_name = user.display_name
        
        # 実行時に最新の団体名リストをDBから取得
        allowed_orgs = get_allowed_orgs() # DBから取得！
        
        # 結果メッセージ用変数
        result_msg = ""
        is_success = False

        # --- ロジック ---
        match = re.search(r'[@＠](.+)$', display_name)
        
        if not match:
            result_msg = f'⚠️ 名前に「@団体名」がありません。\nニックネームを「名前@団体名」にしてから再度押してください。'
        else:
            org_name = match.group(1).strip()

            if org_name not in allowed_orgs: # DBから取得したリストと照合
                result_msg = f'🚫 団体名「{org_name}」は登録されていません。管理者に連絡してください。'
            else:
                # ... (既存のロール/チャンネル処理ロジックはそのまま) ...
                # 1. ロール処理
                role = discord.utils.get(guild.roles, name=org_name)
                created_new_role = False

                if not role:
                    try:
                        role = await guild.create_role(name=org_name, mentionable=True)
                        created_new_role = True
                        await ensure_org_channel(guild, role, org_name)
                    except discord.Forbidden:
                        result_msg = '❌ エラー: Botにロールを作成する権限がありません。'
                        
                # 2. 付与処理（ロールが存在する場合のみ進む）
                if role:
                    if role not in user.roles:
                        try:
                            await user.add_roles(role)
                            result_msg = f'✨ ロール「{role.name}」を付与しました！'
                            if created_new_role:
                                result_msg += ' (新規作成)'
                            is_success = True
                        except discord.Forbidden:
                            result_msg = '❌ エラー: ロール付与の権限がありません。Botの順位を確認してください。'
                    else:
                        result_msg = f'✅ 既にロール「{role.name}」を持っています。'
                        is_success = True

        # --- 1. ユーザーへの返信 (自分だけ見える) ---
        await interaction.followup.send(result_msg, ephemeral=True)

        # --- 2. 管理者への報告 (指定チャンネルに書き込む) ---
        if is_success: 
            log_channel = discord.utils.get(guild.text_channels, name=LOG_CHANNEL_NAME)
            if log_channel:
                embed = discord.Embed(title="🤖 自動処理ログ", color=discord.Color.green())
                embed.add_field(name="実行者", value=f"{user.mention} ({user.display_name})", inline=False)
                embed.add_field(name="結果", value=result_msg, inline=False)
                await log_channel.send(embed=embed)

async def ensure_org_channel(guild, role, org_name):
    # (既存のチャンネル作成ロジックは変更なし)
    channel_name = org_name.lower().replace(" ", "-")
    existing_channel = discord.utils.get(guild.text_channels, name=channel_name)

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(read_messages=False),
        guild.me: discord.PermissionOverwrite(read_messages=True),
        role: discord.PermissionOverwrite(read_messages=True)
    }

    if existing_channel:
        await existing_channel.edit(overwrites=overwrites)
    else:
        try:
            await guild.create_text_channel(channel_name, overwrites=overwrites)
        except:
            pass 

# --- Bot起動・コマンド ---

@bot.event
async def on_ready():
    if engine:
        # データベースにテーブルが存在しない場合、ここで作成
        Base.metadata.create_all(engine)
        print("DBテーブル構造を確認・作成しました。")
    
    bot.add_view(RoleCheckView())
    print(f'{bot.user} 起動完了')


@bot.command()
async def panel(ctx):
    # (コマンドロジックは変更なし)
    embed = discord.Embed(
        title="所属確認・ロール付与",
        description="下のボタンを押すと、名前に応じたロールと個室を自動で用意します。",
        color=discord.Color.blue()
    )
    await ctx.send(embed=embed, view=RoleCheckView())

@bot.command()
async def add_org(ctx, org_name: str):
    if not Session:
        await ctx.send('❌ データベース接続が確立されていません。')
        return
        
    session = Session()
    try:
        # DBに新しい団体名を追加
        if session.query(OrgSettings).filter_by(org_name=org_name).first():
            await ctx.send(f'⚠️ 団体名「{org_name}」は既に登録されています。')
        else:
            new_org = OrgSettings(org_name=org_name)
            session.add(new_org)
            session.commit()
            await ctx.send(f'✅ 団体名リスト（DB）に「{org_name}」を追加しました。')
    except Exception as e:
        session.rollback()
        await ctx.send(f'❌ DBへの書き込みに失敗しました: {e}')
    finally:
        session.close()

@bot.command()
async def list_orgs(ctx):
    allowed_orgs_list = get_allowed_orgs() # DBから取得
    if allowed_orgs_list:
        await ctx.send(f'📋 **登録済み団体名 (DBから取得):**\n' + "\n".join(allowed_orgs_list))
    else:
        await ctx.send('現在登録されている団体名はありません。（またはDB接続エラー）')

# --- 最終起動処理 ---
keep_alive()

# トークン取得は既に上のほうで行われているため、ここではBotの実行のみ
if DISCORD_TOKEN:
    try:
        bot.run(DISCORD_TOKEN)
    except discord.errors.LoginFailure:
        print("FATAL ERROR: 無効なトークンが設定されています。", file=sys.stderr)
    except Exception as e:
        print(f"FATAL ERROR: 予期せぬエラーが発生しました: {e}", file=sys.stderr)
# else の処理は上のほうで sys.exit(1) により実行済み