import os
import json
import time
import asyncio
import discord
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv

# Load environment variables
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

ROLE_UNRANKED = 1521811657901346876
ROLE_BRONZE = 1521811105616363683
ROLE_SILVER = 1521811149748834364
ROLE_GOLD = 1521811185035509830
ROLE_PLATINUM = 1521811708081864814
ROLE_DIAMOND = 1521811749970378922
ROLE_MASTER = 1521811770405027880
ROLE_TOP3 = 1521811809341018235
ROLE_PREV_TOP3 = 1521836585996255393
ROLE_MODERATOR = 1521811029687009330

LEADERBOARD_CHANNEL_ID = 1521810973571416164
QUEUE_CHANNEL_ID = 1521843742493900820
MOD_VERIFY_CHANNEL_ID = 1521848313144672307
DB_FILE = "database.json"

# Rank tier configuration mapping name, minimum ELO, and role ID
TIERS = [
    {"name": "Unranked", "min_elo": 0, "role_id": ROLE_UNRANKED},
    {"name": "Bronze", "min_elo": 200, "role_id": ROLE_BRONZE},
    {"name": "Silver", "min_elo": 400, "role_id": ROLE_SILVER},
    {"name": "Gold", "min_elo": 600, "role_id": ROLE_GOLD},
    {"name": "Platinum", "min_elo": 800, "role_id": ROLE_PLATINUM},
    {"name": "Diamond", "min_elo": 1000, "role_id": ROLE_DIAMOND},
    {"name": "Master", "min_elo": 1200, "role_id": ROLE_MASTER}
]

# Set up intents
intents = discord.Intents.default()
intents.members = True
intents.message_content = True


class EloBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)
        self.db = {}
        self.active_queues = {}  # queue_name -> {"Team A": [user_ids], "Team B": [user_ids]}
        self.active_matches = {}  # match_id -> match details
        self.disputed_players = {}  # user_id -> match_id (Tracks active player DMs to forward proof)
        self.queue_sizes = {
            "1v1": 1,
            "2v2": 2,
            "3v3_elim": 3,
            "4v4_dom": 4,
            "4v4_elim": 4
        }
        self.load_database()

    def load_database(self):
        if os.path.exists(DB_FILE):
            try:
                with open(DB_FILE, "r") as f:
                    self.db = json.load(f)
            except Exception as e:
                print(f"Error loading database: {e}")
                self.db = {"users": {}, "matches": [], "leaderboard_msg_id": None}
        else:
            self.db = {"users": {}, "matches": [], "leaderboard_msg_id": None}

        # Schema compatibility checks
        if "users" not in self.db:
            self.db["users"] = {}
        if "matches" not in self.db:
            self.db["matches"] = []
        if "leaderboard_msg_id" not in self.db:
            self.db["leaderboard_msg_id"] = None

    def save_database(self):
        try:
            with open(DB_FILE, "w") as f:
                json.dump(self.db, f, indent=4)
        except Exception as e:
            print(f"Error saving database: {e}")

    def get_user_data(self, user_id: str):
        u_id = str(user_id)
        if u_id not in self.db["users"]:
            self.db["users"][u_id] = {
                "elo": 0,
                "ubisoft_ign": None,
                "wins": 0,
                "losses": 0,
                "matchups": {},  # opponent_id -> list of match timestamps
                "frozen_matchups": {},  # opponent_id -> list of freeze timestamps
                "current_rank": "Unranked",
                "demotion_strikes": 0
            }
            self.save_database()

        # Schema conversion compatibility checks
        user_data = self.db["users"][u_id]
        if "matchups" not in user_data:
            user_data["matchups"] = {}
        if "frozen_matchups" not in user_data:
            user_data["frozen_matchups"] = {}
        if "current_rank" not in user_data:
            user_data["current_rank"] = self.get_natural_rank_name(user_data.get("elo", 0))
        if "demotion_strikes" not in user_data:
            user_data["demotion_strikes"] = 0
            self.save_database()

        return user_data

    def get_natural_rank_name(self, elo: int) -> str:
        """Determines the rank tier name based strictly on raw numerical ELO."""
        for tier in reversed(TIERS):
            if elo >= tier["min_elo"]:
                return tier["name"]
        return "Unranked"

    def record_matchup(self, player_id: str, opponent_id: str) -> bool:
        """
        Records matchmaking frequency between players.
        Resets after 3 hours. Returns True if total matches exceeds 5.
        """
        p_id = str(player_id)
        o_id = str(opponent_id)

        p_data = self.get_user_data(p_id)
        history = p_data["matchups"].get(o_id, [])

        # Schema conversion if legacy database is using integer counts
        if isinstance(history, int):
            history = []

        current_time = time.time()
        history.append(current_time)

        # Filter out matches older than 3 hours (3 * 3600 seconds)
        three_hours_ago = current_time - 10800
        active_history = [ts for ts in history if ts >= three_hours_ago]

        p_data["matchups"][o_id] = active_history
        self.save_database()

        # If they played more than 5 times in the last 3 hours, freeze ELO
        return len(active_history) > 5

    async def record_frozen_event(self, player_id: str, opponent_id: str, guild: discord.Guild):
        """Tracks ELO freeze occurrences and warns moderators if repeated within 24 hours."""
        p_id = str(player_id)
        o_id = str(opponent_id)

        p_data = self.get_user_data(p_id)
        frozen_history = p_data.get("frozen_matchups", {}).get(o_id, [])

        if isinstance(frozen_history, int):
            frozen_history = []

        current_time = time.time()
        frozen_history.append(current_time)

        # Filter for freezes within the last 24 hours (86400 seconds)
        one_day_ago = current_time - 86400
        active_frozen = [ts for ts in frozen_history if ts >= one_day_ago]

        if "frozen_matchups" not in p_data:
            p_data["frozen_matchups"] = {}
        p_data["frozen_matchups"][o_id] = active_frozen
        self.save_database()

        # Trigger moderator warning starting from the 2nd time being frozen today
        if len(active_frozen) >= 2:
            mod_channel = guild.get_channel(MOD_VERIFY_CHANNEL_ID)
            if not mod_channel:
                try:
                    mod_channel = await guild.fetch_channel(MOD_VERIFY_CHANNEL_ID)
                except Exception:
                    pass

            if mod_channel:
                alert_embed = discord.Embed(
                    title="⚠️ SUSPICIOUS ACTIVITY: POTENTIAL BOOSTING ⚠️",
                    description=(
                        f"**Player:** <@{p_id}>\n"
                        f"**Opponent:** <@{o_id}>\n\n"
                        f"This player has had their ELO frozen against this specific opponent **{len(active_frozen)} times** in the last 24 hours.\n"
                        f"Please monitor their matchmaking behavior closely."
                    ),
                    color=discord.Color.red()
                )
                await mod_channel.send(
                    content=f"🔔 <@&{ROLE_MODERATOR}> **Boosting Alert!**",
                    embed=alert_embed
                )

    async def send_frozen_dm(self, user_id: str):
        """Informs the player that their ELO adjustments are frozen to prevent boosting."""
        try:
            user_target = await self.fetch_user(int(user_id))
            if user_target:
                dm_embed = discord.Embed(
                    title="⚖️ ELO Adjustment Frozen",
                    description=(
                        "Your ELO adjustments for this match have been frozen.\n"
                        "This protective system is automatically in place to avoid boosting and preserve competitive integrity."
                    ),
                    color=discord.Color.orange()
                )
                await user_target.send(embed=dm_embed)
        except Exception as e:
            print(f"Warning: Could not DM user {user_id} regarding ELO freeze. Error: {e}")

    def update_user_elo(self, user_id: str, new_elo: int, won: bool):
        u_id = str(user_id)
        data = self.get_user_data(u_id)

        # Limit numerical ELO strictly between 0 and 2000
        data["elo"] = max(0, min(2000, new_elo))
        if won:
            data["wins"] += 1
        else:
            data["losses"] += 1

        self.process_rank_and_demotion(u_id, won)
        self.save_database()

    def process_rank_and_demotion(self, user_id: str, won: bool):
        """Applies dynamic demotion protection checks to prevent sudden tier drops."""
        u_id = str(user_id)
        data = self.get_user_data(u_id)
        current_elo = data["elo"]
        current_rank = data.get("current_rank", "Unranked")

        # Locate indices inside our configuration mapping
        current_rank_idx = 0
        for i, tier in enumerate(TIERS):
            if tier["name"] == current_rank:
                current_rank_idx = i
                break

        natural_rank_name = self.get_natural_rank_name(current_elo)
        natural_rank_idx = 0
        for i, tier in enumerate(TIERS):
            if tier["name"] == natural_rank_name:
                natural_rank_idx = i
                break

        if natural_rank_idx > current_rank_idx:
            # Clear strikes and immediately promote
            data["current_rank"] = natural_rank_name
            data["demotion_strikes"] = 0
        elif natural_rank_idx == current_rank_idx:
            # Safe within range, reset strikes
            data["demotion_strikes"] = 0
        else:
            # Player is numerically below the threshold floor of their saved rank
            if won:
                # Any victory clears the consecutive losses safety shield strikes!
                data["demotion_strikes"] = 0
            else:
                # Record a consecutive loss strike
                data["demotion_strikes"] += 1
                if data["demotion_strikes"] >= 3:
                    # Demotion protection broken! Update to true lower rank
                    data["current_rank"] = natural_rank_name
                    data["demotion_strikes"] = 0

    async def setup_hook(self):
        await self.tree.sync()
        print("Slash commands synced successfully!")

    def get_team_average_elo_without_outliers(self, team_uids: list) -> int:
        """Calculates refined team average ELO by removing highest & lowest outliers."""
        elos = [self.get_user_data(uid)["elo"] for uid in team_uids]
        if len(elos) <= 2:
            return sum(elos) // len(elos) if elos else 0

        sorted_elos = sorted(elos)
        filtered_elos = sorted_elos[1:-1]
        return sum(filtered_elos) // len(filtered_elos)

    async def update_persistent_leaderboard(self, guild: discord.Guild):
        """Assembles and displays the active server ELO leaderboard in the persistent text channel."""
        channel = guild.get_channel(LEADERBOARD_CHANNEL_ID)
        if not channel:
            try:
                channel = await guild.fetch_channel(LEADERBOARD_CHANNEL_ID)
            except Exception:
                print(f"Error: Dedicated Leaderboard channel with ID {LEADERBOARD_CHANNEL_ID} not found.")
                return

        all_users = []
        for u_id, data in self.db["users"].items():
            try:
                member = guild.get_member(int(u_id))
                if not member:
                    try:
                        member = await guild.fetch_member(int(u_id))
                    except Exception:
                        continue
                if member:
                    all_users.append(
                        (member, data.get("elo", 0), data.get("wins", 0), data.get("current_rank", "Unranked")))
            except ValueError:
                continue

        # Sort all players descending
        all_users.sort(key=lambda x: x[1], reverse=True)
        top_3_uids = [user[0].id for user in all_users[:3] if user[1] > 0]

        ranks_map = {
            "🏆 TOP 3": [],
            "👑 MASTER (1200 - 2000 ELO)": [],
            "💎 DIAMOND (1000 - 1199 ELO)": [],
            "🥇 PLATINUM (800 - 999 ELO)": [],
            "🏅 GOLD (600 - 799 ELO)": [],
            "⚔️ SILVER (400 - 599 ELO)": [],
            "🛡️ BRONZE (200 - 399 ELO)": [],
            "🌱 UNRANKED (0 - 199 ELO)": []
        }

        for member, elo, wins, current_rank in all_users:
            line = f"• **{member.display_name}** — `{elo} ELO` ({wins} Wins)"
            if member.id in top_3_uids:
                ranks_map["🏆 TOP 3"].append(line)
            elif current_rank == "Master":
                ranks_map["👑 MASTER (1200 - 2000 ELO)"].append(line)
            elif current_rank == "Diamond":
                ranks_map["💎 DIAMOND (1000 - 1199 ELO)"].append(line)
            elif current_rank == "Platinum":
                ranks_map["🥇 PLATINUM (800 - 999 ELO)"].append(line)
            elif current_rank == "Gold":
                ranks_map["🏅 GOLD (600 - 799 ELO)"].append(line)
            elif current_rank == "Silver":
                ranks_map["⚔️ SILVER (400 - 599 ELO)"].append(line)
            elif current_rank == "Bronze":
                ranks_map["🛡️ BRONZE (200 - 399 ELO)"].append(line)
            else:
                ranks_map["🌱 UNRANKED (0 - 199 ELO)"].append(line)

        embed = discord.Embed(
            title="⚔️ FOR HONOR COMPETITIVE LEADERBOARD ⚔️",
            description="Auto-updates in real-time when match verifications complete.",
            color=discord.Color.gold()
        )

        for rank_title, lines in ranks_map.items():
            if lines:
                embed.add_field(name=rank_title, value="\n".join(lines), inline=False)

        msg_id = self.db.get("leaderboard_msg_id")
        updated = False
        if msg_id:
            try:
                msg = await channel.fetch_message(int(msg_id))
                await msg.edit(embed=embed)
                updated = True
            except Exception:
                pass

        if not updated:
            new_msg = await channel.send(embed=embed)
            self.db["leaderboard_msg_id"] = str(new_msg.id)
            self.save_database()

    async def execute_match_resolution(self, guild: discord.Guild, match_id: str, winner_team: str,
                                       is_moderated: bool = False) -> discord.Embed:
        """Central database and role manager. Updates user records, rolls out updates, and notifies players."""
        match = self.active_matches.get(match_id)
        if not match:
            raise ValueError("Match is either not active or has already been resolved.")

        team_a_ids = match["Team A"]
        team_b_ids = match["Team B"]

        refined_avg_a = self.get_team_average_elo_without_outliers(team_a_ids)
        refined_avg_b = self.get_team_average_elo_without_outliers(team_b_ids)

        results_log = []
        player_dm_payload = {}  # uid -> {old_elo, new_elo, change, is_win, is_frozen}

        # Match Tie Resolution
        if winner_team == "Tie":
            for uid in team_a_ids + team_b_ids:
                old_elo = self.get_user_data(uid)["elo"]
                results_log.append(f"<@{uid}>: **{old_elo}** ➔ **{old_elo}** (0) *(🤝 Draw/Cancel)*")
                player_dm_payload[uid] = {"old_elo": old_elo, "new_elo": old_elo, "change": 0, "is_win": None,
                                          "is_frozen": False}

        # Team A Win Calculations
        elif winner_team == "Team A":
            for uid in team_a_ids:
                boosting_detected = False
                for opponent in team_b_ids:
                    if self.record_matchup(uid, opponent):
                        boosting_detected = True
                        await self.record_frozen_event(uid, opponent, guild)

                old_elo = self.get_user_data(uid)["elo"]
                if boosting_detected:
                    new_elo = old_elo
                    change = 0
                    results_log.append(
                        f"<@{uid}>: **{old_elo}** ➔ **{new_elo}** (+0) *(⚠️ ELO frozen against opponent to avoid boosting)*")
                    await self.send_frozen_dm(uid)
                else:
                    new_elo = calculate_elo_change(old_elo, refined_avg_b, won=True)
                    change = new_elo - old_elo
                    self.update_user_elo(uid, new_elo, won=True)
                    results_log.append(f"<@{uid}>: **{old_elo}** ➔ **{new_elo}** (+{change})")

                user_data = self.get_user_data(uid)
                player_dm_payload[uid] = {"old_elo": old_elo, "new_elo": new_elo, "change": change, "is_win": True,
                                          "is_frozen": boosting_detected}
                member = guild.get_member(int(uid)) or await guild.fetch_member(int(uid))
                if member:
                    await assign_rank_roles(guild, member, user_data.get("current_rank", "Unranked"))

            for uid in team_b_ids:
                boosting_detected = False
                for opponent in team_a_ids:
                    if self.record_matchup(uid, opponent):
                        boosting_detected = True
                        await self.record_frozen_event(uid, opponent, guild)

                old_elo = self.get_user_data(uid)["elo"]
                if boosting_detected:
                    new_elo = old_elo
                    change = 0
                    results_log.append(
                        f"<@{uid}>: **{old_elo}** ➔ **{new_elo}** (0) *(⚠️ ELO frozen against opponent to avoid boosting)*")
                    await self.send_frozen_dm(uid)
                else:
                    new_elo = calculate_elo_change(old_elo, refined_avg_a, won=False)
                    change = new_elo - old_elo
                    self.update_user_elo(uid, new_elo, won=False)
                    results_log.append(f"<@{uid}>: **{old_elo}** ➔ **{new_elo}** ({change})")

                user_data = self.get_user_data(uid)
                player_dm_payload[uid] = {"old_elo": old_elo, "new_elo": new_elo, "change": change, "is_win": False,
                                          "is_frozen": boosting_detected}
                member = guild.get_member(int(uid)) or await guild.fetch_member(int(uid))
                if member:
                    await assign_rank_roles(guild, member, user_data.get("current_rank", "Unranked"))

        # Team B Win Calculations
        else:
            for uid in team_b_ids:
                boosting_detected = False
                for opponent in team_a_ids:
                    if self.record_matchup(uid, opponent):
                        boosting_detected = True
                        await self.record_frozen_event(uid, opponent, guild)

                old_elo = self.get_user_data(uid)["elo"]
                if boosting_detected:
                    new_elo = old_elo
                    change = 0
                    results_log.append(
                        f"<@{uid}>: **{old_elo}** ➔ **{new_elo}** (+0) *(⚠️ ELO frozen against opponent to avoid boosting)*")
                    await self.send_frozen_dm(uid)
                else:
                    new_elo = calculate_elo_change(old_elo, refined_avg_a, won=True)
                    change = new_elo - old_elo
                    self.update_user_elo(uid, new_elo, won=True)
                    results_log.append(f"<@{uid}>: **{old_elo}** ➔ **{new_elo}** (+{change})")

                user_data = self.get_user_data(uid)
                player_dm_payload[uid] = {"old_elo": old_elo, "new_elo": new_elo, "change": change, "is_win": True,
                                          "is_frozen": boosting_detected}
                member = guild.get_member(int(uid)) or await guild.fetch_member(int(uid))
                if member:
                    await assign_rank_roles(guild, member, user_data.get("current_rank", "Unranked"))

            for uid in team_a_ids:
                boosting_detected = False
                for opponent in team_b_ids:
                    if self.record_matchup(uid, opponent):
                        boosting_detected = True
                        await self.record_frozen_event(uid, opponent, guild)

                old_elo = self.get_user_data(uid)["elo"]
                if boosting_detected:
                    new_elo = old_elo
                    change = 0
                    results_log.append(
                        f"<@{uid}>: **{old_elo}** ➔ **{new_elo}** (0) *(⚠️ ELO frozen against opponent to avoid boosting)*")
                    await self.send_frozen_dm(uid)
                else:
                    new_elo = calculate_elo_change(old_elo, refined_avg_b, won=False)
                    change = new_elo - old_elo
                    self.update_user_elo(uid, new_elo, won=False)
                    results_log.append(f"<@{uid}>: **{old_elo}** ➔ **{new_elo}** ({change})")

                user_data = self.get_user_data(uid)
                player_dm_payload[uid] = {"old_elo": old_elo, "new_elo": new_elo, "change": change, "is_win": False,
                                          "is_frozen": boosting_detected}
                member = guild.get_member(int(uid)) or await guild.fetch_member(int(uid))
                if member:
                    await assign_rank_roles(guild, member, user_data.get("current_rank", "Unranked"))

        # Clear dispute trackers so their DM channel triggers are deleted
        for uid in team_a_ids + team_b_ids:
            if str(uid) in self.disputed_players:
                del self.disputed_players[str(uid)]

        # Recalculate Top 3 holdings
        await recalculate_top_three(guild)

        # Log match to historical DB list
        self.db["matches"].append({
            "match_id": match_id,
            "type": match["type"],
            "team_a": team_a_ids,
            "team_b": team_b_ids,
            "winner": winner_team
        })
        self.save_database()

        # Update dynamic ranking leaderboards in channel
        await self.update_persistent_leaderboard(guild)

        # Delete active match tracking cache
        del self.active_matches[match_id]

        for player_uid, log_data in player_dm_payload.items():
            try:
                user_target = await self.fetch_user(int(player_uid))
                if user_target:
                    user_data = self.get_user_data(player_uid)
                    rank_tier = user_data.get("current_rank", "Unranked")
                    strikes = user_data.get("demotion_strikes", 0)

                    outcome_text = "🤝 Match Cancelled (Tie)"
                    if log_data["is_win"] is True:
                        outcome_text = "🏆 Winner"
                    elif log_data["is_win"] is False:
                        outcome_text = "💀 Defeat"

                    change_symbol = "+" if log_data["change"] >= 0 else ""

                    # Distinguish custom embed depending on review source
                    title_msg = f"⚖️ Disputed Match #{match_id} Resolved" if is_moderated else f"⚖️ Match #{match_id} Results"
                    desc_msg = "A Moderator has manually verified the outcome." if is_moderated else "Your match results have been computed."

                    dm_embed = discord.Embed(
                        title=title_msg,
                        description=desc_msg,
                        color=discord.Color.dark_purple()
                    )

                    if log_data["is_frozen"]:
                        dm_embed.add_field(name="Outcome", value="⚠️ **ELO Frozen (Anti-Boosting Protection)**",
                                           inline=False)
                    else:
                        dm_embed.add_field(name="Outcome", value=f"**{outcome_text}**", inline=False)

                    dm_embed.add_field(name="Previous ELO", value=f"`{log_data['old_elo']}` ELO", inline=True)
                    dm_embed.add_field(name="Current ELO", value=f"`{log_data['new_elo']}` ELO", inline=True)
                    dm_embed.add_field(name="Net ELO Adjustment", value=f"**{change_symbol}{log_data['change']}**",
                                       inline=True)

                    if strikes > 0:
                        dm_embed.add_field(name="Rank Tier",
                                           value=f"**{rank_tier}** (⚠️ Protection Shield Active: {strikes}/3 Losses)",
                                           inline=False)
                    else:
                        dm_embed.add_field(name="Rank Tier", value=f"**{rank_tier}**", inline=False)

                    await user_target.send(embed=dm_embed)
            except Exception as ex_dm:
                print(f"Warning: Could not DM match resolution update to user ID {player_uid}. Error: {ex_dm}")

        embed = discord.Embed(
            title=f"🏆 Match #{match_id} Resolved!",
            description=f"**Winner Outcome:** **{winner_team}**\n\n**ELO Updates:**\n" + "\n".join(results_log),
            color=discord.Color.green()
        )
        return embed


# Instantiate the Bot instance globally, BEFORE any decorators or events run
bot = EloBot()


def calculate_elo_change(player_elo: int, opponent_avg_elo: int, won: bool) -> int:
    """Calculates updated ELO modifications, enforcing a 'flat wall' once players hit 1200 ELO."""
    expected = 1 / (1 + 10 ** ((opponent_avg_elo - player_elo) / 400))
    actual = 1.0 if won else 0.0

    if player_elo < 1200:
        # Standard ELO computations
        k_factor = 32
        change = round(k_factor * (actual - expected))
    else:
        # Master's Flat Wall scaling bounds (1200 - 2000 ELO range)
        if won:
            if player_elo > opponent_avg_elo:
                k_factor = 6
                change = max(1, round(k_factor * (actual - expected)))
            else:
                k_factor = 24
                change = max(5, round(k_factor * (actual - expected)))
        else:
            if player_elo > opponent_avg_elo:
                k_factor = 64
                change = min(-15, round(k_factor * (actual - expected)))
            else:
                k_factor = 24
                change = min(-5, round(k_factor * (actual - expected)))

    # Prevent dropping below zero ELO and bound ELO cap at 2000
    return max(0, min(2000, player_elo + change))


async def assign_rank_roles(guild: discord.Guild, member: discord.Member, current_rank: str):
    """Syncs roles dynamically on server members based on current ELO ratings."""
    roles_to_remove = [
        ROLE_UNRANKED, ROLE_BRONZE, ROLE_SILVER, ROLE_GOLD,
        ROLE_PLATINUM, ROLE_DIAMOND, ROLE_MASTER
    ]

    target_role_id = ROLE_UNRANKED
    for tier in TIERS:
        if tier["name"] == current_rank:
            target_role_id = tier["role_id"]
            break

    try:
        current_role_ids = [role.id for role in member.roles]
        if target_role_id not in current_role_ids:
            target_role = guild.get_role(target_role_id)
            if target_role:
                await member.add_roles(target_role)

        for role_id in roles_to_remove:
            if role_id != target_role_id and role_id in current_role_ids:
                old_role = guild.get_role(role_id)
                if old_role:
                    await member.remove_roles(old_role)
    except Exception as e:
        print(f"Failed to update roles for {member.display_name}: {e}")


async def recalculate_top_three(guild: discord.Guild):
    """Grants Top 3 custom role flags to current peak ELO scorers."""
    all_users = []
    for u_id, data in bot.db["users"].items():
        try:
            member = guild.get_member(int(u_id)) or await guild.fetch_member(int(u_id))
            if member:
                all_users.append((member, data["elo"]))
        except Exception:
            continue

    all_users.sort(key=lambda x: x[1], reverse=True)
    top_3_members = [user[0] for user in all_users[:3] if user[1] > 0]

    top3_role = guild.get_role(ROLE_TOP3)
    if not top3_role:
        return

    # Clear outdated top roles
    for member in guild.members:
        if top3_role in member.roles and member not in top_3_members:
            try:
                await member.remove_roles(top3_role)
            except Exception:
                pass

    # Ensure correct assignment
    for member in top_3_members:
        if top3_role not in member.roles:
            try:
                await member.add_roles(top3_role)
            except Exception:
                pass


# ================== INTERACTIVE INTERFACES & VIEWS ==================

class UbisoftIgnModal(discord.ui.Modal, title="Register Ubisoft IGN"):
    ubisoft_ign = discord.ui.TextInput(
        label="Ubisoft IGN",
        placeholder="Enter your exact Ubisoft Username",
        required=True,
        max_length=50
    )

    def __init__(self, bot_instance, user_id, on_success_callback):
        super().__init__()
        self.bot = bot_instance
        self.user_id = str(user_id)
        self.on_success_callback = on_success_callback

    async def on_submit(self, interaction: discord.Interaction):
        ign = self.ubisoft_ign.value.strip()
        user_data = self.bot.get_user_data(self.user_id)
        user_data["ubisoft_ign"] = ign
        self.bot.save_database()

        await interaction.response.send_message(f"Successfully registered your Ubisoft IGN as **{ign}**!",
                                                ephemeral=True)
        await self.on_success_callback(interaction)


class ModVerifyView(discord.ui.View):
    """Verification view controllers posted inside moderator channel to resolve match disputes."""

    def __init__(self, bot_instance, match_id: str):
        super().__init__(timeout=None)
        self.bot = bot_instance
        self.match_id = match_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        has_mod = any(role.id == ROLE_MODERATOR for role in interaction.user.roles)
        if not has_mod:
            await interaction.response.send_message("❌ Error: Only moderators can resolve disputes.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Team A Won", style=discord.ButtonStyle.success, custom_id="mod_team_a_won")
    async def mod_team_a_won(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        try:
            embed = await self.bot.execute_match_resolution(interaction.guild, self.match_id, "Team A",
                                                            is_moderated=True)
            embed.title = f"✅ Match #{self.match_id} Resolved by {interaction.user.display_name}"
            await interaction.message.edit(embed=embed, view=None)
        except Exception as e:
            await interaction.followup.send(f"❌ Error while resolving match: {str(e)}", ephemeral=True)

    @discord.ui.button(label="Team B Won", style=discord.ButtonStyle.danger, custom_id="mod_team_b_won")
    async def mod_team_b_won(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        try:
            embed = await self.bot.execute_match_resolution(interaction.guild, self.match_id, "Team B",
                                                            is_moderated=True)
            embed.title = f"✅ Match #{self.match_id} Resolved by {interaction.user.display_name}"
            await interaction.message.edit(embed=embed, view=None)
        except Exception as e:
            await interaction.followup.send(f"❌ Error while resolving match: {str(e)}", ephemeral=True)

    @discord.ui.button(label="Tie / Cancel", style=discord.ButtonStyle.secondary, custom_id="mod_tie_cancel")
    async def mod_tie_cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        try:
            embed = await self.bot.execute_match_resolution(interaction.guild, self.match_id, "Tie", is_moderated=True)
            embed.title = f"🤝 Match #{self.match_id} Resolved as Tie by {interaction.user.display_name}"
            await interaction.message.edit(embed=embed, view=None)
        except Exception as e:
            await interaction.followup.send(f"❌ Error while resolving match: {str(e)}", ephemeral=True)


class MatchReportView(discord.ui.View):
    def __init__(self, bot_instance, match_id: str):
        super().__init__(timeout=None)
        self.bot = bot_instance
        self.match_id = match_id
        self.votes = {}

    @discord.ui.button(label="Report Team A Won", style=discord.ButtonStyle.success, custom_id="report_team_a")
    async def team_a_won(self, interaction: discord.Interaction, button: discord.ui.Button):
        match = self.bot.active_matches.get(self.match_id)
        if not match:
            await interaction.response.send_message("This match has already been resolved.", ephemeral=True)
            return

        user_id = str(interaction.user.id)
        if user_id not in match["Team A"] and user_id not in match["Team B"]:
            await interaction.response.send_message("You are not part of this match!", ephemeral=True)
            return

        self.votes[user_id] = "Team A"
        await interaction.response.send_message("You voted for **Team A** winning. Waiting for verification...",
                                                ephemeral=True)
        await self.check_votes(interaction)

    @discord.ui.button(label="Report Team B Won", style=discord.ButtonStyle.danger, custom_id="report_team_b")
    async def team_b_won(self, interaction: discord.Interaction, button: discord.ui.Button):
        match = self.bot.active_matches.get(self.match_id)
        if not match:
            await interaction.response.send_message("This match has already been resolved.", ephemeral=True)
            return

        user_id = str(interaction.user.id)
        if user_id not in match["Team A"] and user_id not in match["Team B"]:
            await interaction.response.send_message("You are not part of this match!", ephemeral=True)
            return

        self.votes[user_id] = "Team B"
        await interaction.response.send_message("You voted for **Team B** winning. Waiting for verification...",
                                                ephemeral=True)
        await self.check_votes(interaction)

    async def check_votes(self, interaction: discord.Interaction):
        match = self.bot.active_matches.get(self.match_id)
        if not match:
            return
        team_a = match["Team A"]
        team_b = match["Team B"]

        team_a_votes = [self.votes[uid] for uid in team_a if uid in self.votes]
        team_b_votes = [self.votes[uid] for uid in team_b if uid in self.votes]

        # Automatic agreement checks
        if "Team A" in team_a_votes and "Team A" in team_b_votes:
            embed = await self.bot.execute_match_resolution(interaction.guild, self.match_id, "Team A",
                                                            is_moderated=False)
            await interaction.message.edit(embed=embed, view=None)
        elif "Team B" in team_a_votes and "Team B" in team_b_votes:
            embed = await self.bot.execute_match_resolution(interaction.guild, self.match_id, "Team B",
                                                            is_moderated=False)
            await interaction.message.edit(embed=embed, view=None)

        # Conflict / Dispute Detected
        elif len(self.votes) == (len(team_a) + len(team_b)):
            mod_channel = interaction.guild.get_channel(MOD_VERIFY_CHANNEL_ID)
            if not mod_channel:
                try:
                    mod_channel = await interaction.guild.fetch_channel(MOD_VERIFY_CHANNEL_ID)
                except Exception:
                    pass

            team_a_mentions = " ".join([f"<@{uid}>" for uid in team_a])
            team_b_mentions = " ".join([f"<@{uid}>" for uid in team_b])

            mod_embed = discord.Embed(
                title=f"⚠️ Match Dispute! Match #{self.match_id}",
                description=(
                    f"**Mode:** {match['type'].upper().replace('_', ' ')}\n\n"
                    f"🔵 **Team A Players:** {team_a_mentions}\n"
                    f"🔴 **Team B Players:** {team_b_mentions}\n\n"
                    f"A dispute has occurred because both teams reported conflicting outcomes. "
                    f"Please review and verify using the moderator controllers below."
                ),
                color=discord.Color.red()
            )

            if mod_channel:
                mod_view = ModVerifyView(self.bot, self.match_id)
                await mod_channel.send(
                    content=f"🔔 <@&{ROLE_MODERATOR}> New match dispute needs resolution!",
                    embed=mod_embed,
                    view=mod_view
                )

            # Register disputed players and DM them requesting screenshots/videos as proof
            for uid in team_a + team_b:
                self.bot.disputed_players[str(uid)] = self.match_id
                try:
                    user_target = await self.bot.fetch_user(int(uid))
                    if user_target:
                        dm_dispute_embed = discord.Embed(
                            title=f"⚠️ Match #{self.match_id} Disputed!",
                            description=(
                                f"There was a conflict in the reported outcomes of your recent match.\n\n"
                                f"**Please provide proof of your victory.**\n"
                                f"Reply directly to this DM by sending:\n"
                                f"• Scoreboard / Victory screen screenshots (Photos)\n"
                                f"• Clip links or files (Videos)\n"
                                f"• Any written statements\n\n"
                                f"All content you type or upload to this bot DM will be compiled and forwarded directly "
                                f"to the **Moderator Review Team** in <#{MOD_VERIFY_CHANNEL_ID}>."
                            ),
                            color=discord.Color.red()
                        )
                        await user_target.send(embed=dm_dispute_embed)
                except Exception as ex_dm:
                    print(f"Warning: Could not DM match dispute proof request to player ID {uid}. Error: {ex_dm}")

            # Terminate voting menu in the public chatroom
            player_embed = discord.Embed(
                title=f"⚠️ Match #{self.match_id} Dispute Registered!",
                description=(
                    "You voted differently on the match outcome. "
                    "This lobby is closed and forwarded to the **Moderator Team** "
                    f"in <#{MOD_VERIFY_CHANNEL_ID}> for review and manual verification."
                ),
                color=discord.Color.orange()
            )
            await interaction.message.edit(embed=player_embed, view=None)


class QueueJoinView(discord.ui.View):
    def __init__(self, bot_instance, queue_name: str):
        super().__init__(timeout=None)
        self.bot = bot_instance
        self.queue_name = queue_name

        if queue_name not in self.bot.active_queues:
            self.bot.active_queues[queue_name] = {"Team A": [], "Team B": []}

    def get_rank_index_by_member(self, member: discord.Member, elo: int) -> int:
        """Determines rank index mapping from 0 (Unranked) to 7 (Top 3)."""
        if any(role.id == ROLE_TOP3 for role in member.roles):
            return 7

        user_data = self.bot.get_user_data(member.id)
        current_rank = user_data.get("current_rank", "Unranked")

        for i, tier in enumerate(TIERS):
            if tier["name"] == current_rank:
                return i
        return 0

    def get_rank_name_by_index(self, index: int) -> str:
        names = ["Unranked", "Bronze", "Silver", "Gold", "Platinum", "Diamond", "Master", "Top 3"]
        if 0 <= index < len(names):
            return names[index]
        return "Unknown"

    async def verify_rank_compatibility(self, member: discord.Member) -> tuple[bool, str]:
        """Checks if the joining member is within range of the queue's anchor player."""
        q = self.bot.active_queues[self.queue_name]
        current_players = q["Team A"] + q["Team B"]
        if not current_players:
            return True, ""

        anchor_id = current_players[0]
        anchor_data = self.bot.get_user_data(anchor_id)
        anchor_elo = anchor_data.get("elo", 0)

        anchor_member = member.guild.get_member(int(anchor_id))
        if not anchor_member:
            try:
                anchor_member = await member.guild.fetch_member(int(anchor_id))
            except Exception:
                return True, ""

        joining_elo = self.bot.get_user_data(member.id).get("elo", 0)

        anchor_rank = self.get_rank_index_by_member(anchor_member, anchor_elo)
        joining_rank = self.get_rank_index_by_member(member, joining_elo)

        max_delta = 1 if self.queue_name == "1v1" else 2

        if abs(joining_rank - anchor_rank) > max_delta:
            min_allowed = max(0, anchor_rank - max_delta)
            max_allowed = min(7, anchor_rank + max_delta)
            allowed_names = [self.get_rank_name_by_index(i) for i in range(min_allowed, max_allowed + 1)]
            allowed_str = ", ".join(allowed_names)

            anchor_name = self.get_rank_name_by_index(anchor_rank)
            joining_name = self.get_rank_name_by_index(joining_rank)

            return False, (
                f"❌ **Rank Mismatch!**\n"
                f"The first player in this queue is **{anchor_name}** (<@{anchor_id}>).\n"
                f"Because this is a **{self.queue_name.upper().replace('_', ' ')}** queue, the allowed rank range is ±{max_delta} rank(s).\n"
                f"Your rank is **{joining_name}**.\n"
                f"Allowed ranks to join: **{allowed_str}**"
            )
        return True, ""

    def get_queue_embed(self) -> discord.Embed:
        q = self.bot.active_queues[self.queue_name]
        size = self.bot.queue_sizes[self.queue_name]

        team_a_mentions = [f"<@{uid}>" for uid in q["Team A"]]
        team_b_mentions = [f"<@{uid}>" for uid in q["Team B"]]

        while len(team_a_mentions) < size:
            team_a_mentions.append("Empty Slot")
        while len(team_b_mentions) < size:
            team_b_mentions.append("Empty Slot")

        embed = discord.Embed(
            title=f"⚔ Matchmaking Queue: {self.queue_name.upper().replace('_', ' ')}",
            description=f"Select your team below! Once both teams are full ({size} vs {size}), players will be automatically matched and notified.",
            color=discord.Color.blurple()
        )
        embed.add_field(name=f"🔵 Team A ({len(q['Team A'])}/{size})", value="\n".join(team_a_mentions), inline=True)
        embed.add_field(name=f"🔴 Team B ({len(q['Team B'])}/{size})", value="\n".join(team_b_mentions), inline=True)
        return embed

    @discord.ui.button(label="Join Team A", style=discord.ButtonStyle.primary, custom_id="join_team_a")
    async def join_team_a(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.process_join(interaction, "Team A")

    @discord.ui.button(label="Join Team B", style=discord.ButtonStyle.danger, custom_id="join_team_b")
    async def join_team_b(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.process_join(interaction, "Team B")

    @discord.ui.button(label="Leave Queue", style=discord.ButtonStyle.secondary, custom_id="leave_queue")
    async def leave_queue(self, interaction: discord.Interaction, button: discord.ui.Button):
        user_id = str(interaction.user.id)
        q = self.bot.active_queues[self.queue_name]

        removed = False
        if user_id in q["Team A"]:
            q["Team A"].remove(user_id)
            removed = True
        elif user_id in q["Team B"]:
            q["Team B"].remove(user_id)
            removed = True

        if removed:
            await interaction.response.send_message("You have successfully left the queue.", ephemeral=True)
            await interaction.message.edit(embed=self.get_queue_embed())
        else:
            await interaction.response.send_message("You are not currently in this queue.", ephemeral=True)

    async def process_join(self, interaction: discord.Interaction, team: str):
        user_id = str(interaction.user.id)
        user_data = self.bot.get_user_data(user_id)
        q = self.bot.active_queues[self.queue_name]
        max_size = self.bot.queue_sizes[self.queue_name]

        other_team = "Team B" if team == "Team A" else "Team A"
        if user_id in q[other_team]:
            q[other_team].remove(user_id)

        if user_id in q[team]:
            await interaction.response.send_message("You are already on this team!", ephemeral=True)
            return

        if len(q[team]) >= max_size:
            await interaction.response.send_message(f"**{team}** is already full!", ephemeral=True)
            return

        # Check rank compatibility
        is_compatible, err_msg = await self.verify_rank_compatibility(interaction.user)
        if not is_compatible:
            await interaction.response.send_message(err_msg, ephemeral=True)
            return

        if not user_data.get("ubisoft_ign"):
            async def on_success(modal_interaction: discord.Interaction):
                # Re-verify compatibility inside modal execution (prevents race-conditions)
                is_compat, err_m = await self.verify_rank_compatibility(modal_interaction.user)
                if not is_compat:
                    await modal_interaction.response.send_message(err_m, ephemeral=True)
                    return

                q[team].append(user_id)
                await modal_interaction.message.edit(embed=self.get_queue_embed())
                await self.check_queue_full(modal_interaction)

            modal = UbisoftIgnModal(self.bot, user_id, on_success)
            await interaction.response.send_modal(modal)
            return

        q[team].append(user_id)
        await interaction.response.send_message(f"Joined **{team}**!", ephemeral=True)
        await interaction.message.edit(embed=self.get_queue_embed())
        await self.check_queue_full(interaction)

    async def check_queue_full(self, interaction: discord.Interaction):
        q = self.bot.active_queues[self.queue_name]
        max_size = self.bot.queue_sizes[self.queue_name]

        if len(q["Team A"]) == max_size and len(q["Team B"]) == max_size:
            match_id = str(len(self.bot.db["matches"]) + len(self.bot.active_matches) + 1)

            self.bot.active_matches[match_id] = {
                "type": self.queue_name,
                "Team A": list(q["Team A"]),
                "Team B": list(q["Team B"])
            }

            team_a_public_details = []
            team_b_public_details = []

            team_a_dm_details = []
            team_b_dm_details = []
            all_uids = q["Team A"] + q["Team B"]

            # Format list displays. Hide Ubisoft IGN from the public server text channel.
            for uid in q["Team A"]:
                ign = self.bot.get_user_data(uid)["ubisoft_ign"]
                team_a_public_details.append(f"🔵 **Team A** | <@{uid}>")
                team_a_dm_details.append(f"🔵 **Team A** | <@{uid}> - Ubisoft IGN: `{ign}`")

            for uid in q["Team B"]:
                ign = self.bot.get_user_data(uid)["ubisoft_ign"]
                team_b_public_details.append(f"🔴 **Team B** | <@{uid}>")
                team_b_dm_details.append(f"🔴 **Team B** | <@{uid}> - Ubisoft IGN: `{ign}`")

            self.bot.active_queues[self.queue_name] = {"Team A": [], "Team B": []}
            await interaction.message.edit(embed=self.get_queue_embed())

            # Display the Match Ready announcement without Ubisoft Names (IGN)
            report_embed = discord.Embed(
                title=f"⚔ Match Ready: Match #{match_id} ({self.queue_name.upper().replace('_', ' ')})",
                description="Your matchmaking lobby is ready! Use player-submitted buttons below to report the winner.",
                color=discord.Color.gold()
            )
            report_embed.add_field(name="🔵 Team A Details", value="\n".join(team_a_public_details), inline=False)
            report_embed.add_field(name="🔴 Team B Details", value="\n".join(team_b_public_details), inline=False)

            report_view = MatchReportView(self.bot, match_id)
            await interaction.channel.send(content="".join([f"<@{uid}> " for uid in all_uids]), embed=report_embed,
                                           view=report_view)

            # Send private DMs WITH Ubisoft IGNs so players can add each other
            dm_embed = discord.Embed(
                title=f"⚔ Match #{match_id} is Ready!",
                description="Your matchmaking lobby has been configured. Add each other on Ubisoft Connect:",
                color=discord.Color.purple()
            )
            dm_embed.add_field(name="🔵 Team A", value="\n".join(team_a_dm_details), inline=False)
            dm_embed.add_field(name="🔴 Team B", value="\n".join(team_b_dm_details), inline=False)

            for uid in all_uids:
                try:
                    user = await self.bot.fetch_user(int(uid))
                    if user:
                        await user.send(embed=dm_embed)
                except Exception:
                    pass


# ================== DISCORD BOT EVENTS ==================

@bot.event
async def on_message(message: discord.Message):
    # Process commands normally
    if message.author.bot:
        return

    # Check if the message is inside direct messages
    if message.guild is None:
        user_id_str = str(message.author.id)
        if user_id_str in bot.disputed_players:
            match_id = bot.disputed_players[user_id_str]

            # Fetch the dedicated moderator verification channel
            mod_channel = bot.get_channel(MOD_VERIFY_CHANNEL_ID)
            if not mod_channel:
                try:
                    mod_channel = await bot.fetch_channel(MOD_VERIFY_CHANNEL_ID)
                except Exception:
                    pass

            if mod_channel:
                # Prepare direct proof statement embed
                proof_embed = discord.Embed(
                    title=f"📁 Dispute Proof Submitted - Match #{match_id}",
                    description=f"**Player Tag:** <@{message.author.id}>\n**Username:** {message.author.display_name}",
                    color=discord.Color.orange()
                )

                if message.content:
                    proof_embed.add_field(name="Player Statement", value=message.content, inline=False)

                files = []
                if message.attachments:
                    attachment_links = []
                    for attachment in message.attachments:
                        attachment_links.append(f"[{attachment.filename}]({attachment.url})")
                        try:
                            # Re-download the attachment from discord server to local client as a File upload
                            file_obj = await attachment.to_file()
                            files.append(file_obj)
                        except Exception as file_ex:
                            print(f"Warning: Failed to parse user attachment file: {file_ex}")

                    proof_embed.add_field(name="Captured Attachments", value="\n".join(attachment_links), inline=False)

                # Send collected player statement/files directly to moderator verify channel
                await mod_channel.send(
                    content=f"🔔 **Match #{match_id}** Dispute Proof Update from <@{message.author.id}>:",
                    embed=proof_embed,
                    files=files
                )
                await message.channel.send(
                    "✅ **Your proof has been received!** It has been forwarded directly to the moderators reviewing your match.")
                return
            else:
                await message.channel.send(
                    "❌ Internal Error: Could not connect to the moderator review system. Please notify an administrator.")
                return

    await bot.process_commands(message)


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user.name} - ID: {bot.user.id}")
    print("---------------------------------------------")


@bot.event
async def on_member_join(member: discord.Member):
    unranked_role = member.guild.get_role(ROLE_UNRANKED)
    if unranked_role:
        try:
            await member.add_roles(unranked_role)
        except Exception:
            pass


# ================== DISCORD BOT COMMANDS ==================

@bot.tree.command(name="queue", description="Enter matchmaking queues for For Honor game modes")
@app_commands.choices(mode=[
    app_commands.Choice(name="1v1 Duel", value="1v1"),
    app_commands.Choice(name="2v2 Brawl", value="2v2"),
    app_commands.Choice(name="3v3 Elimination", value="3v3_elim"),
    app_commands.Choice(name="4v4 Dominion", value="4v4_dom"),
    app_commands.Choice(name="4v4 Elimination", value="4v4_elim"),
])
async def queue(interaction: discord.Interaction, mode: app_commands.Choice[str]):
    # Enforce queue command execution channel limit
    if interaction.channel_id != QUEUE_CHANNEL_ID:
        await interaction.response.send_message(
            f"❌ Matchmaking queues can only be entered in the <#{QUEUE_CHANNEL_ID}> channel!",
            ephemeral=True
        )
        return

    unranked_role = interaction.guild.get_role(ROLE_UNRANKED)
    if unranked_role and unranked_role not in interaction.user.roles:
        try:
            await interaction.user.add_roles(unranked_role)
        except Exception:
            pass

    view = QueueJoinView(bot, mode.value)
    await interaction.response.send_message(embed=view.get_queue_embed(), view=view)


@bot.tree.command(name="profile", description="View your current ELO, rank tier, Ubisoft IGN and stats")
async def profile(interaction: discord.Interaction, user: discord.Member = None):
    target_user = user or interaction.user
    user_data = bot.get_user_data(target_user.id)

    ign = user_data.get("ubisoft_ign") or "*Not set. Join a queue to register.*"
    elo = user_data.get("elo", 0)
    wins = user_data.get("wins", 0)
    losses = user_data.get("losses", 0)
    total_games = wins + losses
    winrate = (wins / total_games * 100) if total_games > 0 else 0

    rank_name = user_data.get("current_rank", "Unranked")
    strikes = user_data.get("demotion_strikes", 0)

    # Format a visual warning badge on profile card if safety shield has strikes
    rank_display_string = f"**{rank_name}**"
    if strikes > 0:
        rank_display_string = f"**{rank_name}** (⚠️ Protection Shield Active: `{strikes}/3` Losses)"

    embed = discord.Embed(
        title=f"🛡 Player Profile - {target_user.display_name}",
        color=discord.Color.blue()
    )
    embed.set_thumbnail(url=target_user.display_avatar.url)
    embed.add_field(name="Ubisoft IGN", value=ign, inline=False)
    embed.add_field(name="ELO Rating", value=f"**{elo}** ({rank_display_string})", inline=True)
    embed.add_field(name="Win / Loss", value=f"🏆 {wins}W - 💀 {losses}L", inline=True)
    embed.add_field(name="Win Rate", value=f"📊 {winrate:.1f}%", inline=True)

    await interaction.response.send_message(embed=embed)


@bot.command(name="restart-season")
async def restart_season(ctx: commands.Context):
    # Check if user has moderator role ID directly
    has_mod = any(role.id == ROLE_MODERATOR for role in ctx.author.roles)
    if not has_mod:
        await ctx.send("❌ Error: You do not have the required Moderator role to execute this command.")
        return

    status_msg = await ctx.send("⏳ Processing seasonal reset... Please wait.")

    top3_role = ctx.guild.get_role(ROLE_TOP3)
    prev_top3_role = ctx.guild.get_role(ROLE_PREV_TOP3)
    unranked_role = ctx.guild.get_role(ROLE_UNRANKED)

    rank_roles = [
        ctx.guild.get_role(ROLE_BRONZE),
        ctx.guild.get_role(ROLE_SILVER),
        ctx.guild.get_role(ROLE_GOLD),
        ctx.guild.get_role(ROLE_PLATINUM),
        ctx.guild.get_role(ROLE_DIAMOND),
        ctx.guild.get_role(ROLE_MASTER),
        top3_role
    ]

    top3_members = []
    if top3_role:
        top3_members = [m for m in ctx.guild.members if top3_role in m.roles]

    for member in top3_members:
        try:
            if prev_top3_role:
                await member.add_roles(prev_top3_role)
            if top3_role:
                await member.remove_roles(top3_role)
        except Exception:
            pass

    # Clear stats, reset demotion protective parameters, and clear matchup histories
    for u_id in bot.db["users"]:
        bot.db["users"][u_id]["elo"] = 0
        bot.db["users"][u_id]["wins"] = 0
        bot.db["users"][u_id]["losses"] = 0
        bot.db["users"][u_id]["matchups"] = {}  # clear matchup counts too
        bot.db["users"][u_id]["current_rank"] = "Unranked"
        bot.db["users"][u_id]["demotion_strikes"] = 0
        if "frozen_matchups" in bot.db["users"][u_id]:
            bot.db["users"][u_id]["frozen_matchups"] = {}
    bot.save_database()

    for member in ctx.guild.members:
        if member.bot:
            continue

        try:
            for r in rank_roles:
                if r and r in member.roles:
                    await member.remove_roles(r)

            if unranked_role and unranked_role not in member.roles:
                await member.add_roles(unranked_role)
        except Exception:
            pass

    # Refresh persistent leaderboard
    await bot.update_persistent_leaderboard(ctx.guild)

    await status_msg.edit(
        content="🏆 **The Season has been successfully reset!**\n- All players' ELO and stats have been reset to 0.\n- Previous Top 3 players have been awarded their legacy roles.\n- Everyone has been demoted back to Unranked status with full protection shields.")


if __name__ == "__main__":
    if not TOKEN:
        print("CRITICAL ERROR: No Discord Bot Token found in environment variables. Please check your .env file.")
    else:
        bot.run(TOKEN)