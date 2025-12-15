import os
from flask import Flask
from threading import Thread

# --- Flask Webサーバーの設定 ---

app = Flask('')

# ルートURL（"/"）にアクセスがあったときに実行される関数
@app.route('/')
def home():
    # 監視サービスに「生きているよ」と伝えるための応答
    return "Bot is alive!"

# Flaskサーバーを別スレッドで実行するための関数
# Botのメイン処理をブロックしないようにするため
def run_server():
    # Renderが環境変数 'PORT' で指定するポートを使用する
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)

# サーバー起動をBotの起動前に呼び出す関数
def keep_alive():
    t = Thread(target=run_server)
    t.start()
import discord
from discord.ext import commands
import re # 正規表現を使うためのライブラリ

# --- 設定 ---
LOG_CHANNEL_NAME = '管理ログ'

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix='!', intents=intents)
allowed_orgs = set()

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
        
        # 結果メッセージ用変数
        result_msg = ""
        is_success = False

        # --- ロジック ---
        match = re.search(r'[@＠](.+)$', display_name)
        
        if not match:
            result_msg = f'⚠️ 名前に「@団体名」がありません。\nニックネームを「名前@団体名」にしてから再度押してください。'
        else:
            org_name = match.group(1).strip()

            if org_name not in allowed_orgs:
                result_msg = f'🚫 団体名「{org_name}」は登録されていません。管理者に連絡してください。'
            else:
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
        # 成功したとき、またはエラーのときなど、報告したい内容を調整できます
        if is_success: 
            log_channel = discord.utils.get(guild.text_channels, name=LOG_CHANNEL_NAME)
            if log_channel:
                embed = discord.Embed(title="🤖 自動処理ログ", color=discord.Color.green())
                embed.add_field(name="実行者", value=f"{user.mention} ({user.display_name})", inline=False)
                embed.add_field(name="結果", value=result_msg, inline=False)
                await log_channel.send(embed=embed)

async def ensure_org_channel(guild, role, org_name):
    """ロール専用チャンネルの確認・作成（interaction不要版）"""
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
            pass # ログ出力は呼び出し元で行うなど調整可

# --- Bot起動・コマンド ---

@bot.event
async def on_ready():
    bot.add_view(RoleCheckView())
    print(f'{bot.user} 起動完了')

@bot.command()
async def panel(ctx):
    embed = discord.Embed(
        title="所属確認・ロール付与",
        description="下のボタンを押すと、名前に応じたロールと個室を自動で用意します。",
        color=discord.Color.blue()
    )
    await ctx.send(embed=embed, view=RoleCheckView())

@bot.command()
async def add_org(ctx, org_name: str):
    allowed_orgs.add(org_name)
    await ctx.send(f'✅ 団体名リストに「{org_name}」を追加しました。')

@bot.command()
async def list_orgs(ctx):
    if allowed_orgs:
        await ctx.send(f'📋 **登録済み団体名:**\n' + "\n".join(allowed_orgs))
    else:
        await ctx.send('現在登録されている団体名はありません。')

DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN") 
if DISCORD_TOKEN:
    try:
        # トークンを使用してBotを起動
        bot.run(DISCORD_TOKEN)
    except discord.errors.LoginFailure:
        print("FATAL ERROR: 無効なトークンが設定されています。")
    except Exception as e:
        print(f"FATAL ERROR: 予期せぬエラーが発生しました: {e}")
else:
    print("FATAL ERROR: DISCORD_TOKEN 環境変数が設定されていません。")