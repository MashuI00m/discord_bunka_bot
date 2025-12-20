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

Base.metadata.create_all(engine)

# --- Discord Bot 実装 ---
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

class RoleCheckView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
    
    @discord.ui.button(label="所属を同期する", style=discord.ButtonStyle.primary, custom_id="v_final_sync")
    async def sync(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        
        # ニックネーム判定
        match = re.search(r'[@＠](.+)$', interaction.user.display_name)
        if not match:
            return await interaction.followup.send("⚠️ 名前を「名前@団体名」にしてください。")
        
        org_key = match.group(1).strip().lower()
        
        session = Session()
        # サーバー設定取得
        config = session.query(ServerConfig).filter_by(guild_id=str(guild.id)).first()
        if not config:
            config = ServerConfig(guild_id=str(guild.id), guild_name=guild.name)
            session.add(config); session.commit(); session.refresh(config)
            
        # 団体検索
        org = session.query(OrgSettings).filter(
            OrgSettings.guild_id == str(guild.id),
            ((OrgSettings.org_name.ilike(org_key)) | (OrgSettings.alias.ilike(org_key)))
        ).first()
        session.close()

        if not org:
            return await interaction.followup.send(f"🚫 「{org_key}」はこのサーバーに登録されていません。!add_org で登録してください。")

        # ロール付与
        org_role = discord.utils.get(guild.roles, name=org.org_name) or await guild.create_role(name=org.org_name)
        await interaction.user.add_roles(org_role)
        
        # 部長ロール判定 (逆転ロジック：基本付与、条件で除外)
        is_proxy = "代理" in interaction.user.display_name
        leader_role = discord.utils.get(guild.roles, name=config.leader_role_name) or await guild.create_role(name=config.leader_role_name)
        
        if not org.exclude_leader and not is_proxy:
            await interaction.user.add_roles(leader_role)
            msg = f"✅ {org.org_name} ＆ {config.leader_role_name} 同期完了"
        else:
            if leader_role in interaction.user.roles:
                await interaction.user.remove_roles(leader_role)
            msg = f"✅ {org.org_name} 同期完了（役職なし）"

        # チャンネル作成 (団体用カテゴリ)
        cat = discord.utils.get(guild.categories, name=config.target_category)
        if cat:
            ch_name = org.org_name.lower().replace(" ", "-")
            if not any(ch_name in c.name.lower() for c in cat.text_channels):
                overwrites = {guild.default_role: discord.PermissionOverwrite(read_messages=False),
                              org_role: discord.PermissionOverwrite(read_messages=True),
                              guild.me: discord.PermissionOverwrite(read_messages=True)}
                await guild.create_text_channel(ch_name, category=cat, overwrites=overwrites)
        
        await interaction.followup.send(msg)

@bot.event
async def on_ready():
    bot.add_view(RoleCheckView())
    print(f"✅ 起動完了: {bot.user}")

@bot.command()
@commands.has_permissions(administrator=True)
async def panel(ctx):
    await ctx.send(f"**{ctx.guild.name} 所属確認**", view=RoleCheckView())

@bot.command()
@commands.has_permissions(administrator=True)
async def add_org(ctx, name: str, alias: str = None, exclude: bool = False):
    s = Session()
    s.add(OrgSettings(guild_id=str(ctx.guild.id), org_name=name, alias=alias, exclude_leader=exclude))
    s.commit(); s.close()
    await ctx.send(f"✅ {name} を登録しました。")

# --- Flask サーバー ---
app = Flask(__name__)
@app.route('/')
def h(): return "ok"
def k():
    Thread(target=lambda: app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000)))).start()
    while True:
        try:
            url = os.environ.get("RENDER_EXTERNAL_URL")
            if url: requests.get(url)
            Session().query(ServerConfig).first()
        except: pass
        time.sleep(300)

if __name__ == "__main__":
    k()
    bot.run(os.environ.get("DISCORD_TOKEN"))