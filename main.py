import os, sys, re, requests, time, datetime
from threading import Thread
from flask import Flask
import discord
from discord.ext import commands
from sqlalchemy import create_engine, Column, String, Boolean, DateTime, Integer
from sqlalchemy.orm import sessionmaker, declarative_base

# --- DB設定 ---
DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL: sys.exit(1)
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

Base.metadata.create_all(engine)

# --- View (デバッグログ付き) ---
class RoleCheckView(discord.ui.View):
    def __init__(self): super().__init__(timeout=None)
    
    @discord.ui.button(label="所属を同期する", style=discord.ButtonStyle.primary, custom_id="v_debug_1")
    async def sync(self, interaction: discord.Interaction, button: discord.ui.Button):
        print(f"DEBUG: ボタンが押されました (User: {interaction.user.display_name})") # ログに出力
        await interaction.response.defer(ephemeral=True)
        
        guild = interaction.guild
        session = Session()
        config = session.query(ServerConfig).filter_by(guild_id=str(guild.id)).first()
        if not config:
            config = ServerConfig(guild_id=str(guild.id), guild_name=guild.name)
            session.add(config); session.commit(); session.refresh(config)
        
        match = re.search(r'[@＠](.+)$', interaction.user.display_name)
        if not match:
            session.close()
            return await interaction.followup.send("⚠️ 名前を「名前@団体名」にしてください。")
        
        org_key = match.group(1).strip().lower()
        org = session.query(OrgSettings).filter(
            OrgSettings.guild_id == str(guild.id),
            ((OrgSettings.org_name.ilike(org_key)) | (OrgSettings.alias.ilike(org_key)))
        ).first()
        session.close()

        if not org:
            return await interaction.followup.send(f"🚫 「{org_key}」は未登録です。!add_org で登録してください。")

        # ロール付与
        role = discord.utils.get(guild.roles, name=org.org_name) or await guild.create_role(name=org.org_name)
        await interaction.user.add_roles(role)
        
        # 部長ロール (代理判定)
        if "代理" not in interaction.user.display_name and not org.exclude_leader:
            l_role = discord.utils.get(guild.roles, name=config.leader_role_name) or await guild.create_role(name=config.leader_role_name)
            await interaction.user.add_roles(l_role)

        # カテゴリ作成
        cat = discord.utils.get(guild.categories, name=config.target_category)
        if cat:
            ch_name = org.org_name.lower().replace(" ", "-")
            if not any(ch_name in c.name.lower() for c in cat.text_channels):
                overwrites = {guild.default_role: discord.PermissionOverwrite(read_messages=False),
                              role: discord.PermissionOverwrite(read_messages=True),
                              guild.me: discord.PermissionOverwrite(read_messages=True)}
                await guild.create_text_channel(ch_name, category=cat, overwrites=overwrites)
        
        await interaction.followup.send(f"✅ {org.org_name} として同期完了！")

# --- Bot 本体 ---
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

@bot.event
async def on_ready():
    bot.add_view(RoleCheckView())
    print(f"✅ Bot is ready: {bot.user.name}")

@bot.command()
@commands.has_permissions(administrator=True)
async def panel(ctx):
    await ctx.send("【認証パネル】", view=RoleCheckView())

@bot.command()
@commands.has_permissions(administrator=True)
async def add_org(ctx, name: str, alias: str = None, exclude: bool = False):
    s = Session()
    s.add(OrgSettings(guild_id=str(ctx.guild.id), org_name=name, alias=alias, exclude_leader=exclude))
    s.commit(); s.close()
    await ctx.send(f"✅ {name} を登録しました。")

# --- インフラ ---
app = Flask(__name__)
@app.route('/')
def h(): return "ok"
def k():
    Thread(target=lambda: app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000)))).start()
    while True:
        try: requests.get(os.environ.get("RENDER_EXTERNAL_URL")); Session().query(ServerConfig).first()
        except: pass
        time.sleep(300)

if __name__ == "__main__":
    k()
    bot.run(os.environ.get("DISCORD_TOKEN"))