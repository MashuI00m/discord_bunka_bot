import os
import sys
import re 
from threading import Thread
from flask import Flask
import discord
from discord.ext import commands
import requests # HTTPリクエスト用
import time     # 時間制御用
import asyncio

# --- DB/SQLAlchemy 関連のインポート ---
import datetime 
from sqlalchemy import create_engine, Column, String, Integer, Boolean, DateTime
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

# --- 定数 ---
LOG_CHANNEL_NAME = '管理ログ'
PROXY_ROLE_NAME = 'Proxy Attendee' 
SHARED_CATEGORY_NAME = '会議室'

# --- DBテーブル定義 ---
class OrgSettings(Base):
    """許可された団体名を保存するテーブル (略称カラムを追加)"""
    __tablename__ = 'allowed_organizations'
    
    id = Column(Integer, primary_key=True)
    org_name = Column(String, unique=True, nullable=False) # 本名 (ロール名)
    alias = Column(String, unique=True, nullable=True)     # 略称 (ニックネーム対応用)

    def __repr__(self):
        return f"<OrgSettings(org_name='{self.org_name}', alias='{self.alias}')>"

class Attendance(Base):
    """出席記録を保存するテーブル"""
    __tablename__ = 'attendance_records'
    
    id = Column(Integer, primary_key=True)
    user_id = Column(String, nullable=False)
    org_name = Column(String, nullable=False)
    is_proxy = Column(Boolean, default=False)
    timestamp = Column(DateTime, default=datetime.datetime.utcnow) 

    def __repr__(self):
        return f"<Attendance(user_id='{self.user_id}', org_name='{self.org_name}', is_proxy={self.is_proxy})>"

# --- Flask Webサーバーの設定 ---
app = Flask(__name__) 

@app.route('/')
def home():
    return "Bot is alive!"

# Bot自身のURLを取得するための環境変数
BOT_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL") 
PING_INTERVAL_SECONDS = 300 # 5分 (300秒) ごとにピンギング

def ping_self():
    """Botの外部URLに定期的にアクセスする関数"""
    if not BOT_EXTERNAL_URL:
        print("WARNING: RENDER_EXTERNAL_URL が設定されていないため、セルフピンギングをスキップします。")
        return

    while True:
        try:
            # Bot自身のWebサーバーにアクセス
            response = requests.get(BOT_EXTERNAL_URL)
            print(f"セルフピンギング実行: ステータスコード {response.status_code}")
        except requests.exceptions.RequestException as e:
            print(f"セルフピンギング中にエラーが発生しました: {e}")
        
        # 5分間待機
        time.sleep(PING_INTERVAL_SECONDS)

def run_server():
    port = int(os.environ.get("PORT", 5000))
    # Flask Webサーバーを起動
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    """Botの起動とは別に、Webサーバーとピンギングループを別スレッドで起動する"""
    # 1. Webサーバー起動スレッド
    server_thread = Thread(target=run_server)
    server_thread.start()

    # 2. セルフピンギングスレッド (新規追加)
    ping_thread = Thread(target=ping_self)
    ping_thread.start()
# ----------------------------------------------------

# --- Bot設定 ---
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
if DISCORD_TOKEN is None:
    print("FATAL ERROR: DISCORD_TOKEN 環境変数が設定されていません。", file=sys.stderr)
    sys.exit(1)

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix='!', intents=intents)

# --- DB操作関数 ---

def get_allowed_orgs_map():
    """DBから許可された団体名マップ {ニックネームの団体名: ロール名} を取得する (修正)"""
    if not Session:
        return {}
    session = Session()
    org_map = {}
    try:
        orgs = session.query(OrgSettings.org_name, OrgSettings.alias).all()
        for org_name, alias in orgs:
            # 本名をマップに追加 {本名: 本名}
            org_map[org_name.lower()] = org_name 
            # 略称があればマップに追加 {略称: 本名}
            if alias:
                org_map[alias.lower()] = org_name
        return org_map
    except Exception as e:
        print(f"DB Error during fetching organizations: {e}")
        return {}
    finally:
        session.close()

def record_attendance(user_id: str, org_name: str, is_proxy: bool):
    if not Session:
        return False
    session = Session()
    try:
        new_record = Attendance(
            user_id=user_id,
            org_name=org_name,
            is_proxy=is_proxy
        )
        session.add(new_record)
        session.commit()
        return True
    except Exception as e:
        session.rollback()
        print(f"DB Error during attendance recording: {e}")
        return False
    finally:
        session.close()

def delete_attendance_records(user_id_to_delete=None, is_proxy_status=None):
    """
    指定された条件に一致する Attendance レコードをDBから削除する関数
    """
    if not Session:
        return 0 
    
    session = Session()
    deleted_count = 0
    
    try:
        query = session.query(Attendance)
        
        # 削除条件の組み立て
        if user_id_to_delete:
            query = query.filter(Attendance.user_id == user_id_to_delete)
        
        if is_proxy_status is not None:
            query = query.filter(Attendance.is_proxy == is_proxy_status)
            
        # 削除実行 (条件がない場合は、コマンド実行側で制御するため、ここでは実行しない)
        if user_id_to_delete or is_proxy_status is not None:
            deleted_count = query.delete(synchronize_session=False)
            session.commit()
            return deleted_count
        else:
             return 0
        
    except Exception as e:
        session.rollback()
        print(f"DB DELETE Error: {e}")
        return -1 
        
    finally:
        session.close()

# --- ユーティリティ関数（変更なし） ---

async def get_or_create_proxy_role(guild):
    role = discord.utils.get(guild.roles, name=PROXY_ROLE_NAME)
    if role is None:
        try:
            role = await guild.create_role(name=PROXY_ROLE_NAME, color=discord.Color.dark_red(), mentionable=False)
        except discord.Forbidden:
            print("ERROR: Bot lacks permission to create the Proxy Attendee role.")
            return None
    return role

async def ensure_org_channel(guild, role, org_name):
    channel_name = org_name.lower().replace(" ", "-")
    existing_channel = discord.utils.get(guild.text_channels, name=channel_name)

    proxy_role = discord.utils.get(guild.roles, name=PROXY_ROLE_NAME)
    
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(read_messages=False),
        guild.me: discord.PermissionOverwrite(read_messages=True),
        role: discord.PermissionOverwrite(read_messages=True)
    }
    
    if proxy_role:
        overwrites[proxy_role] = discord.PermissionOverwrite(read_messages=False)

    if existing_channel:
        await existing_channel.edit(overwrites=overwrites)
    else:
        try:
            await guild.create_text_channel(channel_name, overwrites=overwrites)
        except:
            pass 

# ---------------------------------------------------------
# ボタンの定義 (既存のロールチェック View)
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
        
        org_map = get_allowed_orgs_map() 
        proxy_role = await get_or_create_proxy_role(guild)
        
        # 1. ニックネームの代理フラグと団体名抽出
        is_proxy_in_name = bool(re.search(r'(代理|だいり)', display_name, flags=re.IGNORECASE))
        cleaned_name = re.sub(r'(代理|だいり)', '', display_name, flags=re.IGNORECASE).strip()
        match = re.search(r'[@＠](.+)$', cleaned_name)
        
        result_msg = ""
        is_success = False
        removed_roles = [] 
        
        if not match:
            result_msg = f'⚠️ 名前に「@団体名」がありません。\nニックネームを「名前@団体名」にしてから再度押してください。'
            await interaction.followup.send(result_msg, ephemeral=True)
            return
        
        # ニックネームから抽出された団体名（略称の可能性あり）
        nickname_org_key = match.group(1).strip().lower()

        # 団体名が登録されているかチェックし、登録されている場合はロール名（本名）を取得
        role_org_name = org_map.get(nickname_org_key) 
        
        if not role_org_name:
            result_msg = f'🚫 団体名「{nickname_org_key}」は登録されていません。管理者に連絡してください。'
        else:
            # 2a. 既存の団体ロールの剥奪
            roles_to_remove = []
            
            # DBに登録されている全ての団体ロールをリストアップ
            all_org_names = set(org_map.values())
            
            for user_role in user.roles:
                # ユーザーが持っているロールが団体ロールリストに含まれているかチェック
                is_allowed_org_role = user_role.name in all_org_names

                # 代理の場合: 自分の団体ロールを含む全ての団体ロールを剥奪
                if is_proxy_in_name and is_allowed_org_role:
                    roles_to_remove.append(user_role)
                
                # 通常の場合: 自分の団体ロール(role_org_name)以外で、リストに含まれる団体ロールを剥奪
                elif not is_proxy_in_name and is_allowed_org_role and user_role.name != role_org_name:
                    roles_to_remove.append(user_role)
            
            # ロール剥奪の実行
            for role_to_remove in roles_to_remove:
                try:
                    await user.remove_roles(role_to_remove)
                    removed_roles.append(role_to_remove.name)
                except discord.Forbidden:
                    print(f"ERROR: Bot lacks permission to remove role {role_to_remove.name} from {user.display_name}")
            
            
            if is_proxy_in_name:
                # 2b. Case A: 代理参加 (団体ロールは付与しない)
                if proxy_role:
                    if proxy_role not in user.roles:
                        try:
                            await user.add_roles(proxy_role)
                            result_msg = f'🛡️ ニックネームに「代理」が含まれていたため、**代理参加ロール**を付与しました。団体個室は見えなくなります。'
                            is_success = True
                        except discord.Forbidden:
                            result_msg = '❌ エラー: 代理ロール付与の権限がありません。'
                    else:
                         result_msg = f'🛡️ ニックネームに「代理」が含まれています。（代理ロール既に保持）'
                         is_success = True
                else:
                     result_msg = '❌ 代理ロールが見つからないか作成できませんでした。'

            else:
                # 2c. Case B: 通常参加 (団体ロールを付与)
                
                # 代理ロールの剥奪 (通常参加のため)
                if proxy_role and proxy_role in user.roles:
                    try:
                        await user.remove_roles(proxy_role)
                        removed_roles.append(proxy_role.name)
                    except discord.Forbidden:
                        print(f"ERROR: Bot lacks permission to remove proxy role from {user.display_name}")

                # 団体ロール（本名）の付与
                role = discord.utils.get(guild.roles, name=role_org_name) # 本名のロールを検索
                created_new_role = False

                if not role:
                    try:
                        role = await guild.create_role(name=role_org_name, mentionable=True)
                        created_new_role = True
                        await ensure_org_channel(guild, role, role_org_name)
                    except discord.Forbidden:
                        result_msg = '❌ エラー: Botにロールを作成する権限がありません。'
                        
                if role:
                    if role not in user.roles:
                        try:
                            await user.add_roles(role)
                            result_msg = f'✨ ロール「{role.name}」を付与しました！'
                            if created_new_role:
                                result_msg += ' (新規作成)'
                            is_success = True
                        except discord.Forbidden:
                            result_msg = '❌ エラー: ロール付与の権限がありません。'
                    else:
                        result_msg = f'✅ 既にロール「{role.name}」を持っています。'
                        is_success = True
                
        if removed_roles:
            removal_info = " ".join(removed_roles)
            result_msg += f'\n(🗑️ 以下の不要なロールを剥奪しました: {removal_info})'
            
        await interaction.followup.send(result_msg, ephemeral=True)

        if is_success or removed_roles: 
            log_channel = discord.utils.get(guild.text_channels, name=LOG_CHANNEL_NAME)
            if log_channel:
                embed = discord.Embed(title="🤖 自動処理ログ", color=discord.Color.green())
                embed.add_field(name="実行者", value=f"{user.mention} ({user.display_name})", inline=False)
                embed.add_field(name="結果", value=result_msg, inline=False)
                await log_channel.send(embed=embed)

# ---------------------------------------------------------
# 出席確認のための新しい View
# ---------------------------------------------------------
class AttendanceView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def _handle_checkin(self, interaction: discord.Interaction, is_proxy: bool):
        await interaction.response.defer(ephemeral=True)

        user = interaction.user
        guild = interaction.guild
        display_name = user.display_name
        org_map = get_allowed_orgs_map() 
        
        # ニックネームから団体名を抽出
        cleaned_name = re.sub(r'(代理|だいり)', '', display_name, flags=re.IGNORECASE).strip()
        match = re.search(r'[@＠](.+)$', cleaned_name)

        if not match:
            return await interaction.followup.send('⚠️ 名前に「@団体名」がありません。ニックネームを「名前@団体名」にしてから再度押してください。', ephemeral=True)
        
        # ニックネームから抽出された団体名（略称の可能性あり）
        nickname_org_key = match.group(1).strip().lower()
        role_org_name = org_map.get(nickname_org_key) # 本名のロール名を取得
        
        if not role_org_name:
            return await interaction.followup.send(f'🚫 団体名「{nickname_org_key}」は登録されていません。管理者に連絡してください。', ephemeral=True)

        
        proxy_role = await get_or_create_proxy_role(guild)
        
        result_msg = ""
        log_msg = f"{user.display_name} ({user.id}) が {role_org_name} でチェックイン: "

        if is_proxy:
            if proxy_role and proxy_role not in user.roles:
                try:
                    await user.add_roles(proxy_role)
                    result_msg += '🛡️ 代理参加としてチェックインしました。個室は見えなくなります。'
                    log_msg += '代理参加'
                except discord.Forbidden:
                    result_msg += '❌ 代理ロール付与の権限がありません。'
                    log_msg += '代理参加（権限エラー）'
            else:
                 result_msg += '🛡️ 代理参加としてチェックインしました。（既に代理ロール保持）'
                 log_msg += '代理参加（変更なし）'
        else:
            if proxy_role and proxy_role in user.roles:
                try:
                    await user.remove_roles(proxy_role)
                    result_msg += '✅ 通常参加としてチェックインしました。個室が再表示されます。'
                    log_msg += '通常参加（代理ロール剥奪）'
                except discord.Forbidden:
                    result_msg += '❌ 代理ロール剥奪の権限がありません。'
                    log_msg += '通常参加（権限エラー）'
            else:
                 result_msg += '✅ 通常参加としてチェックインしました。'
                 log_msg += '通常参加'

        # DBに出席記録 (ロール名である role_org_name を使用)
        if record_attendance(str(user.id), role_org_name, is_proxy):
            result_msg += '\n✨ 出席を記録しました。'
        else:
            result_msg += '\n❌ データベースへの出席記録に失敗しました。'
            
        log_channel = discord.utils.get(guild.text_channels, name=LOG_CHANNEL_NAME)
        if log_channel:
            embed = discord.Embed(title="🔔 出席確認ログ", description=log_msg, color=discord.Color.blue())
            embed.add_field(name="実行者", value=f"{user.mention} ({user.display_name})", inline=False)
            embed.add_field(name="団体名", value=role_org_name, inline=True)
            embed.add_field(name="区分", value="代理" if is_proxy else "通常", inline=True)
            embed.add_field(name="タイムスタンプ", value=datetime.datetime.now().strftime('%Y/%m/%d %H:%M:%S'), inline=True)
            await log_channel.send(embed=embed)

        await interaction.followup.send(result_msg, ephemeral=True)


    @discord.ui.button(label="通常参加でチェックイン", style=discord.ButtonStyle.green, custom_id="checkin_regular_button")
    async def checkin_regular_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_checkin(interaction, is_proxy=False)

    @discord.ui.button(label="代理参加でチェックイン (個室非表示)", style=discord.ButtonStyle.red, custom_id="checkin_proxy_button")
    async def checkin_proxy_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_checkin(interaction, is_proxy=True)


# --- Bot起動・コマンド ---

@bot.event
async def on_ready():
    if engine:
        Base.metadata.create_all(engine)
        print("DBテーブル構造を確認・作成しました。")
    
    for guild in bot.guilds:
        proxy_role = await get_or_create_proxy_role(guild)

       # 共有チャンネル権限設定ロジック (変更なし)
        shared_overwrite = discord.PermissionOverwrite(
        read_messages=True, 
        send_messages=True, 
        connect=True,      
        speak=True          
        )

        # DBから登録されている全ての団体名を取得
            
        org_map = get_allowed_orgs_map()
        all_org_names = set(org_map.values()) # 本名のロール名リスト

        for guild in bot.guilds:
            proxy_role = await get_or_create_proxy_role(guild)

        for category in guild.categories:
            if category.name == SHARED_CATEGORY_NAME: # '会議室' カテゴリ
                print(f"会議室カテゴリ ({category.name}) の権限を設定中...")
            
             # 1. @everyone のアクセスを無効化 (セキュリティのため)
                try:
                 await category.set_permissions(guild.default_role, overwrite=discord.PermissionOverwrite(read_messages=False))
                except discord.Forbidden:
                    print(" - ❌ 権限不足によりカテゴリの権限設定ができませんでした。")
                    continue

                # 2. 代理ロールにアクセスを許可
                if proxy_role:
                    try:
                        await category.set_permissions(proxy_role, overwrite=shared_overwrite)
                        print(" - 代理ロールにアクセスを明示的に許可しました。")
                    except discord.Forbidden:
                        pass

                # 3. 団体ロールにアクセスを許可 (ここが新しいロジック)
                for org_name in all_org_names:
                    org_role = discord.utils.get(guild.roles, name=org_name)
                    if org_role:
                        try:
                            await category.set_permissions(org_role, overwrite=shared_overwrite)
                            print(f" - 団体ロール {org_name} にアクセスを許可しました。")
                        except discord.Forbidden:
                            print(f" - ❌ 団体ロール {org_name} の権限設定に失敗しました。")


                print(" - カテゴリ全体の設定完了")
                
                # 4. カテゴリ内のチャンネル権限も同様に設定 (これは既存のロジックを再利用)
                for channel in category.channels:
                    # チャンネルレベルでも @everyone を無効化
                    await channel.set_permissions(guild.default_role, overwrite=discord.PermissionOverwrite(read_messages=False))
                    
                    # 代理ロールにアクセスを許可
                    if proxy_role:
                        await channel.set_permissions(proxy_role, overwrite=shared_overwrite)
                    
                    # 団体ロールにアクセスを許可
                    for org_name in all_org_names:
                        org_role = discord.utils.get(guild.roles, name=org_name)
                        if org_role:
                            await channel.set_permissions(org_role, overwrite=shared_overwrite)

        
    bot.add_view(RoleCheckView())
    bot.add_view(AttendanceView())
    print(f'{bot.user} 起動完了')


@bot.command()
@commands.has_permissions(administrator=True)
async def bulk_proxy_checkin(ctx):
    guild = ctx.guild
    org_map = get_allowed_orgs_map()
    proxy_role = await get_or_create_proxy_role(guild)
    
    if not proxy_role:
        return await ctx.send("❌ 代理ロールの準備ができませんでした。Botの権限を確認してください。")

    processed_count = 0
    proxy_added_count = 0
    proxy_removed_count = 0
    all_org_names = set(org_map.values())
    
    await ctx.send("🤖 サーバーメンバーの一括代理/通常参加チェックを開始します。人数が多い場合、完了まで時間がかかることがあります...")

    for member in guild.members:
        if member.bot:
            continue
            
        display_name = member.display_name
        is_proxy_in_name = bool(re.search(r'(代理|だいり)', display_name, flags=re.IGNORECASE))
        
        cleaned_name = re.sub(r'(代理|だいり)', '', display_name, flags=re.IGNORECASE).strip()
        match = re.search(r'[@＠](.+)$', cleaned_name)

        if match:
            nickname_org_key = match.group(1).strip().lower()
            role_org_name = org_map.get(nickname_org_key)

            if role_org_name:
                org_role = discord.utils.get(guild.roles, name=role_org_name)
                
                if not org_role:
                    try:
                        org_role = await guild.create_role(name=role_org_name, mentionable=True)
                        await ensure_org_channel(guild, org_role, role_org_name)
                    except discord.Forbidden:
                        continue
                
                # 1. 団体ロールの付与/剥奪
                if is_proxy_in_name:
                    # 代理の場合: 団体ロールを剥奪 (持っていたら)
                    if org_role in member.roles:
                        try: await member.remove_roles(org_role)
                        except discord.Forbidden: pass
                else:
                    # 通常の場合: 団体ロールを付与
                    if org_role not in member.roles:
                        try: await member.add_roles(org_role)
                        except discord.Forbidden: pass

                    # 他の団体ロールの剥奪
                    for user_role in member.roles:
                        if user_role.name in all_org_names and user_role.name != role_org_name:
                            try: await member.remove_roles(user_role)
                            except discord.Forbidden: pass


                # 2. 代理ステータスの処理とDB記録
                if is_proxy_in_name:
                    if proxy_role not in member.roles:
                        try:
                            await member.add_roles(proxy_role)
                            proxy_added_count += 1
                        except discord.Forbidden: pass
                        
                    record_attendance(str(member.id), role_org_name, True)
                    processed_count += 1
                    
                else: 
                    if proxy_role in member.roles:
                        try:
                            await member.remove_roles(proxy_role)
                            proxy_removed_count += 1
                        except discord.Forbidden: pass
                        
                    record_attendance(str(member.id), role_org_name, False)
                    processed_count += 1

    report_message = (
        f"✅ **一括代理/通常参加チェックが完了しました。**\n"
        f"・処理されたメンバー数: {processed_count}名\n"
        f"・新たに代理ロールが付与されたメンバー: {proxy_added_count}名\n"
        f"・代理ロールが剥奪されたメンバー: {proxy_removed_count}名\n"
        f"※ DBへの記録も行いました。"
    )
    await ctx.send(report_message)
    log_channel = discord.utils.get(guild.text_channels, name=LOG_CHANNEL_NAME)
    if log_channel:
        await log_channel.send(report_message)


@bot.command()
@commands.has_permissions(administrator=True)
async def delete_attendance(ctx, target: discord.Member = None, proxy_status: str = None):
    """
    Attendance (出席記録) テーブルからレコードを削除する。
    - target: 削除したいユーザー (@ユーザー名)
    - proxy_status: 削除したいステータス ('proxy' または 'regular')
    例: !delete_attendance @User proxy (指定ユーザーの代理記録を削除)
    例: !delete_attendance proxy (全ユーザーの代理記録を削除)
    """
    if not Session:
        return await ctx.send('❌ データベース接続が確立されていません。')

    await ctx.defer()
    user_id = str(target.id) if target else None
    
    is_proxy_delete = None
    if proxy_status and proxy_status.lower() in ('proxy', '代理'):
        is_proxy_delete = True
    elif proxy_status and proxy_status.lower() in ('regular', '通常'):
        is_proxy_delete = False
    elif proxy_status:
        return await ctx.send("❌ `proxy_status`は 'proxy' または 'regular' のみを指定してください。")

    # 削除条件が一つも指定されていない場合はエラー
    if not user_id and is_proxy_delete is None:
        return await ctx.send("❌ **削除を実行するには、対象ユーザー (@ユーザー名) または削除したいステータス ('proxy'/'regular') の少なくとも一方を指定してください。**")

    # ユーザー指定がない（全ユーザー対象）場合のみ確認を求める
    if not target:
        status_text = f"**{'代理' if is_proxy_delete else '通常'}**の出席記録" if is_proxy_delete is not None else "全ての出席記録"
        await ctx.send(f"⚠️ **警告**: 対象ユーザーが指定されていません。全ユーザーの{status_text}が対象になります。続行しますか？ (`yes` / `no`)")
        
        def check(m):
            return m.author == ctx.author and m.channel == ctx.channel and m.content.lower() in ('yes', 'no')
            
        try:
            msg = await bot.wait_for('message', check=check, timeout=30.0)
            if msg.content.lower() == 'no':
                return await ctx.send("操作をキャンセルしました。")
        except asyncio.TimeoutError:
            return await ctx.send("タイムアウトしました。操作をキャンセルします。")
    
    # 削除実行
    deleted_count = delete_attendance_records(user_id, is_proxy_delete)
    
    if deleted_count > 0:
        await ctx.send(f"✅ DBから**{deleted_count}件**の出席記録を削除しました。")
    elif deleted_count == 0:
        await ctx.send("ℹ️ 削除条件に一致するレコードは見つかりませんでした。")
    else:
        await ctx.send("❌ DBの削除中にエラーが発生しました。ログを確認してください。")


@bot.command()
async def panel(ctx):
    embed = discord.Embed(
        title="所属確認・ロール付与",
        description="下のボタンを押すと、名前に応じたロールと個室を自動で用意します。\n**名前に「代理」が含まれている場合、団体ロールは付与されません。**",
        color=discord.Color.blue()
    )
    await ctx.send(embed=embed, view=RoleCheckView())

@bot.command()
async def attend_panel(ctx):
    embed = discord.Embed(
        title="📝 出席確認パネル",
        description="ご自身の参加区分に合わせたボタンを押して、出席を記録してください。\n**最初に `!panel` で団体ロールが付与されている必要があります。**",
        color=discord.Color.orange()
    )
    await ctx.send(embed=embed, view=AttendanceView())

@bot.command()
async def add_org(ctx, org_name: str, alias: str = None):
    """団体名（ロール名）と必要に応じて略称をDBに追加する (修正)"""
    if not Session:
        await ctx.send('❌ データベース接続が確立されていません。')
        return
        
    session = Session()
    try:
        if session.query(OrgSettings).filter_by(org_name=org_name).first():
            await ctx.send(f'⚠️ 団体名「{org_name}」は既に登録されています。')
        elif alias and session.query(OrgSettings).filter_by(alias=alias).first():
             await ctx.send(f'⚠️ 略称「{alias}」は既に他の団体で使用されています。')
        else:
            new_org = OrgSettings(org_name=org_name, alias=alias)
            session.add(new_org)
            session.commit()
            msg = f'✅ 団体名リスト（DB）に「{org_name}」を追加しました。'
            if alias:
                msg += f' (略称: {alias})'
            await ctx.send(msg)
    except Exception as e:
        session.rollback()
        await ctx.send(f'❌ DBへの書き込みに失敗しました: {e}')
    finally:
        session.close()

@bot.command(name='delete_org')
@commands.has_permissions(administrator=True)
async def delete_org(ctx, org_identifier: str):
    """
    団体名（ロール名）または略称を指定して、OrgSettingsから団体設定を削除します。
    """
    
    # データベースから団体情報を取得・削除する関数を呼び出す
    success = delete_org_setting(org_identifier) # この関数は別途定義が必要です

    if success:
        await ctx.send(f"✅ 団体設定 **{org_identifier}** の削除が完了しました。")
    else:
        await ctx.send(f"❌ 団体設定 **{org_identifier}** は見つかりませんでした。本名または略称を確認してください。")

@delete_org.error
async def delete_org_error(ctx, error):
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("❌ 削除したい団体名（本名または略称）を指定してください。例: `!delete_org テニス部`")
    elif isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ このコマンドを実行するには管理者権限が必要です。")
    else:
        print(f"delete_orgコマンドで予期せぬエラー: {error}")

@bot.command()
async def list_orgs(ctx):
    org_map = get_allowed_orgs_map()
    if org_map:
        output = '📋 **登録済み団体名 (本名 / 略称):**\n'
        
        # ロール名（本名）と略称をペアで出力するためにDBから直接取得
        session = Session()
        try:
            orgs = session.query(OrgSettings.org_name, OrgSettings.alias).all()
            for org_name, alias in orgs:
                output += f'- {org_name}'
                if alias:
                    output += f' / {alias}'
                output += '\n'
        except Exception:
            output = '❌ データベースからの読み込みに失敗しました。'
        finally:
            session.close()

        await ctx.send(output)
    else:
        await ctx.send('現在登録されている団体名はありません。（またはDB接続エラー）')

# --- 最終起動処理 ---
keep_alive() 

if DISCORD_TOKEN:
    try:
        bot.run(DISCORD_TOKEN)
    except discord.errors.LoginFailure:
        print("FATAL ERROR: 無効なトークンが設定されています。", file=sys.stderr)
    except Exception as e:
        print(f"FATAL ERROR: 予期せぬエラーが発生しました: {e}", file=sys.stderr)