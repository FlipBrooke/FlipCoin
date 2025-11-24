# main.py — merged + fixes (welcome bonus, request buttons, fixes)
import asyncio
import discord
from dotenv import load_dotenv
from supabase import create_client, Client
import os

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
# Optional: initial bank balance to bootstrap the "Bank" account if it doesn't exist.
# Set as an integer string in .env, e.g. BANK_INITIAL_BALANCE=10000
BANK_INITIAL_BALANCE = int(os.getenv("BANK_INITIAL_BALANCE", "10000"))

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

bot = discord.Bot(intents=discord.Intents.default())

# embed color used everywhere (fallback)
EMBED_COLOR_YELLOW = 0xFFD43B


# ---------------- formatting helpers ----------------
def fmt_commas(num):
    try:
        return f"{int(num):,}"
    except Exception:
        return str(num)


def clean(num):
    try:
        n = float(num)
    except Exception:
        return "0"
    if n == 0:
        return "0"
    sign = "-" if n < 0 else ""
    n = abs(n)
    units = ['', 'k', 'm', 'b', 't']
    idx = 0
    while n >= 1000 and idx < len(units) - 1:
        n /= 1000.0
        idx += 1
    if n >= 100:
        s = f"{int(round(n))}"
    elif n >= 10:
        s = f"{n:.1f}".rstrip('0').rstrip('.')
    else:
        s = f"{n:.1f}".rstrip('0').rstrip('.')
    return f"{sign}{s}{units[idx]}"


# ---------------- DB helpers ----------------
def get_user_row(discord_id: str):
    try:
        resp = supabase.table("users").select("*").eq("discord_id", discord_id).execute()
        if getattr(resp, "data", None) and len(resp.data) > 0:
            return resp.data[0]
        return None
    except Exception as e:
        print("get_user_row error:", e)
        return None


def upsert_user_basic(discord_id: str, username: str, pfp_url: str, color_hex: str):
    """
    Returns True if a new user was inserted, False if an existing user was updated or on error.
    """
    try:
        existing = supabase.table("users").select("discord_id").eq("discord_id", discord_id).execute()
        if getattr(existing, "data", None) and len(existing.data) > 0:
            supabase.table("users").update({"username": username, "pfp": pfp_url, "color": color_hex}).eq("discord_id", discord_id).execute()
            return False
        else:
            # Ensure a balance field exists (default 0)
            supabase.table("users").insert({
                "discord_id": discord_id,
                "username": username,
                "pfp": pfp_url,
                "color": color_hex,
                "balance": 0
            }).execute()
            return True
    except Exception as e:
        print("upsert_user_basic error:", e)
        return False


def ensure_bank_account_exists():
    """
    Ensure a bank account (bot user id) exists in the users table. If missing, optionally create it
    with BANK_INITIAL_BALANCE (from env). Returns bank_id (str) or None if bot.user not available.
    """
    if not bot.user:
        return None
    bank_id = str(bot.user.id)
    try:
        row = get_user_row(bank_id)
        if row:
            return bank_id
        # create the bank user with default name 'Bank' and a neutral color; pfp uses bot avatar if available
        pfp_url = str(bot.user.display_avatar.url) if bot.user else None
        color_hex = f"#{EMBED_COLOR_YELLOW:06x}"
        supabase.table("users").insert({
            "discord_id": bank_id,
            "username": "Bank",
            "pfp": pfp_url,
            "color": color_hex,
            "balance": BANK_INITIAL_BALANCE
        }).execute()
        return bank_id
    except Exception as e:
        print("ensure_bank_account_exists error:", e)
        return bank_id


def log_transaction(from_id: str, to_id: str, amount: int, from_bal: int, to_bal: int, reason: str):
    try:
        supabase.table("transactions").insert({
            "from": from_id,
            "to": to_id,
            "amount": int(amount),
            "from_bal": int(from_bal),
            "to_bal": int(to_bal),
            "status": "complete",
            "reason": reason
        }).execute()
    except Exception as e:
        print("log_transaction error:", e)


# ---------------- atomic RPC + fallback transfer ----------------
def rpc_atomic_send(sender_id: str, recipient_id: str, amount: int, reason: str):
    try:
        resp = supabase.rpc('atomic_send', {
            'sender_id': sender_id,
            'recipient_id': recipient_id,
            'amt': int(amount),
            'reason_text': reason
        }).execute()

        if getattr(resp, "error", None):
            err = resp.error.get("message") if isinstance(resp.error, dict) else str(resp.error)
            raise Exception(err)

        if not getattr(resp, "data", None) or len(resp.data) == 0:
            raise Exception("No response from atomic_send")

        row = resp.data[0]
        from_bal = row.get("from_bal")
        to_bal = row.get("to_bal")
        return int(from_bal), int(to_bal)
    except Exception as e:
        raise


def fallback_transfer(sender_id: str, recipient_id: str, amount: int, reason: str):
    try:
        s = supabase.table("users").select("balance").eq("discord_id", sender_id).execute()
        if not getattr(s, "data", None) or len(s.data) == 0:
            raise Exception("sender_not_found")
        sender_bal = int(s.data[0].get("balance", 0))
        if sender_bal < amount:
            raise Exception("insufficient_funds")

        r = supabase.table("users").select("balance").eq("discord_id", recipient_id).execute()
        if not getattr(r, "data", None) or len(r.data) == 0:
            raise Exception("recipient_not_found")
        recipient_bal = int(r.data[0].get("balance", 0))

        new_sender_bal = sender_bal - amount
        dec = supabase.table("users").update({"balance": new_sender_bal}).eq("discord_id", sender_id).gte("balance", amount).execute()
        if not getattr(dec, "data", None):
            raise Exception("insufficient_funds")

        new_recipient_bal = recipient_bal + amount
        supabase.table("users").update({"balance": new_recipient_bal}).eq("discord_id", recipient_id).execute()

        log_transaction(sender_id, recipient_id, amount, new_sender_bal, new_recipient_bal, reason)
        return new_sender_bal, new_recipient_bal
    except Exception:
        raise


def transfer_money(sender_id: str, recipient_id: str, amount: int, reason: str):
    try:
        return rpc_atomic_send(sender_id, recipient_id, amount, reason)
    except Exception as e:
        msg = str(e).lower()
        if "insufficient" in msg or "sender_not_found" in msg or "recipient_not_found" in msg:
            raise
        try:
            return fallback_transfer(sender_id, recipient_id, amount, reason)
        except Exception:
            # re-raise original RPC exception if fallback also fails
            raise


async def async_transfer(sender_id: str, recipient_id: str, amount: int, reason: str):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, transfer_money, sender_id, recipient_id, amount, reason)


# ---------------- color helper ----------------
def parse_color_hex_to_int(color_hex: str):
    """
    Accepts '#RRGGBB' or 'RRGGBB' and returns an int usable by discord.Color.
    Returns the fallback EMBED_COLOR_YELLOW on parse failure or missing value.
    """
    if not color_hex:
        return EMBED_COLOR_YELLOW
    try:
        s = color_hex.strip()
        if s.startswith("#"):
            s = s[1:]
        if len(s) != 6:
            return EMBED_COLOR_YELLOW
        return int(s, 16)
    except Exception:
        return EMBED_COLOR_YELLOW


def get_embed_color_for_id(discord_id: str):
    """
    Always prefer the user's stored `color` field. If missing or invalid,
    fall back to EMBED_COLOR_YELLOW.
    """
    if not discord_id:
        return EMBED_COLOR_YELLOW
    row = get_user_row(discord_id)
    if not row:
        return EMBED_COLOR_YELLOW
    color_hex = row.get("color")
    return parse_color_hex_to_int(color_hex)


# ---------------- safe interaction helpers ----------------
async def safe_defer(ctx, ephemeral: bool = False) -> bool:
    try:
        if hasattr(ctx, "response") and not ctx.response.is_done():
            await ctx.defer(ephemeral=ephemeral)
            return True
    except Exception as e:
        # common when interaction already responded/expired
        print("safe_defer:", e)
    return False


async def safe_respond(ctx, *args, ephemeral=False, **kwargs):
    """
    Send a reply safely: if already deferred/responded, use followup; else respond.
    If both fail, attempt a minimal ephemeral to the user to avoid silent failures.
    """
    try:
        if hasattr(ctx, "response"):
            if ctx.response.is_done():
                await ctx.followup.send(*args, ephemeral=ephemeral, **kwargs)
            else:
                await ctx.respond(*args, ephemeral=ephemeral, **kwargs)
        else:
            # fallback for raw Interaction-like objects
            await ctx.send(*args, **kwargs)
    except Exception as e:
        print("safe_respond intermediate failure:", e)
        try:
            # best-effort final fallback: send ephemeral directly to the user (if available)
            if hasattr(ctx, "author") and getattr(ctx, "author", None):
                try:
                    await ctx.author.send("The bot attempted to respond but failed to send the full message. Check the server or try again.")
                except Exception:
                    # last resort: do nothing, but log it
                    print("safe_respond final fallback DM failed")
            else:
                print("safe_respond has no author to DM.")
        except Exception as e2:
            print("safe_respond final failure:", e2)


# ---------------- events ----------------
@bot.event
async def on_ready():
    print("loading...")
    try:
        await bot.sync_commands(force=True)
    except Exception as e:
        print("sync_commands error:", e)
    print("BUY BUY BUY! SELL SELL SELL!")


# ---------------- Request view ----------------
class RequestSendButton(discord.ui.View):
    def __init__(self, requester: discord.User, recipient: discord.User, amount: int, reason: str, timeout: float = None):
        super().__init__(timeout=timeout)
        self.requester = requester  # person who will receive money
        self.recipient = recipient  # expected to pay (can accept/deny)
        self.amount = int(amount)
        self.reason = reason

    async def _do_transfer(self, sender_id: str, recipient_id: str, amount: int, reason: str):
        return await async_transfer(sender_id, recipient_id, amount, reason)

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success)
    async def accept(self, button: discord.ui.Button, interaction: discord.Interaction):
        if interaction.user.id != self.recipient.id:
            await safe_respond(interaction, "You are not allowed to accept this request.", ephemeral=True)
            return

        # public followup
        try:
            await interaction.response.defer(ephemeral=False)
        except Exception:
            # ignore if already deferred/responded
            pass

        sender_id = str(interaction.user.id)
        recipient_id = str(self.requester.id)

        if not get_user_row(sender_id) or not get_user_row(recipient_id):
            await interaction.followup.send("Account missing — use /register", ephemeral=True)
            return

        try:
            from_bal, to_bal = await self._do_transfer(sender_id, recipient_id, self.amount, self.reason)
        except Exception as e:
            err = str(e)
            if "insufficient" in err.lower():
                await interaction.followup.send("You don't have enough funds to accept this request.", ephemeral=False)
            else:
                await interaction.followup.send(f"Transaction failed: {err}", ephemeral=False)
            return

        # best-effort delete original request and disable buttons
        try:
            await interaction.message.delete()
        except Exception:
            pass
        self.disable_all_items()
        try:
            await interaction.message.edit(view=self)
        except Exception:
            pass

        embed = discord.Embed(
            title="Transaction Complete",
            color=discord.Color(get_embed_color_for_id(sender_id)),
            description=(
                f"**{interaction.user.display_name} ({clean(from_bal)})** sent **${fmt_commas(self.amount)}** to "
                f"**{self.requester.display_name} ({clean(to_bal)})**\n{self.reason}"
            )
        )
        embed.set_thumbnail(url=self.requester.display_avatar.url)
        await interaction.followup.send(embed=embed, ephemeral=False)

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger)
    async def deny(self, button: discord.ui.Button, interaction: discord.Interaction):
        if interaction.user.id != self.recipient.id:
            await safe_respond(interaction, "You are not allowed to deny this request.", ephemeral=True)
            return

        try:
            await interaction.response.defer(ephemeral=False)
        except Exception:
            pass

        try:
            await interaction.message.delete()
        except Exception:
            pass

        self.disable_all_items()
        try:
            await interaction.message.edit(view=self)
        except Exception:
            pass

        embed = discord.Embed(
            title="Request Denied",
            color=discord.Color(get_embed_color_for_id(str(interaction.user.id))),
            description=(
                f"**{interaction.user.display_name}** denied the request of **${fmt_commas(self.amount)}** "
                f"from **{self.requester.display_name}**\n{self.reason}"
            )
        )
        embed.set_thumbnail(url=self.requester.display_avatar.url)
        await interaction.followup.send(embed=embed, ephemeral=False)


# ---------------- commands ----------------
@bot.slash_command(name="register", description="Create or update your account")
async def register(ctx):
    used_followup = await safe_defer(ctx)
    discord_id = str(ctx.author.id)
    avatar_url = str(ctx.author.display_avatar.url)
    username = ctx.author.name

    # prefer user's role color as initial stored color if they don't have one yet
    role_color_int = ctx.author.color.value if ctx.author.color.value != 0 else EMBED_COLOR_YELLOW
    color_hex = f"#{role_color_int:06x}"

    # detect whether this is a new user or an update
    was_new = upsert_user_basic(discord_id, username, avatar_url, color_hex)

    # after upsert, get the canonical row and stored color
    row = get_user_row(discord_id)
    balance_amount = row.get("balance", 0) if row else 0
    pfp = row.get("pfp") if row else None

    # Use stored color field for embed color (falls back to constant)
    color_int = get_embed_color_for_id(discord_id)

    # Send either welcome (new) or updated message (existing)
    if was_new:
        welcome_embed = discord.Embed(
            title=f"Welcome, {username}!",
            color=discord.Color(color_int),
            description="Your account has been created. 🎉"
        )
    else:
        welcome_embed = discord.Embed(
            title=f"Account updated — {username}",
            color=discord.Color(color_int),
            description="Your account information has been updated."
        )
    if pfp:
        welcome_embed.set_thumbnail(url=pfp)

    await safe_respond(ctx, embed=welcome_embed)

    # If this was a NEW registration, try to give up to 50 coins from the bank
    if was_new:
        bank_id = ensure_bank_account_exists()

        # if no bank account exists or bot user not available, treat as balance 0
        bank_balance = 0
        if bank_id:
            bank_row = get_user_row(bank_id)
            bank_balance = int(bank_row.get("balance", 0)) if bank_row else 0

        # If the bank has at least 50, attempt transfer
        if bank_balance >= 50 and bank_id:
            try:
                # Fixed: format the reason to include username
                reason_text = f"{username}'s beta-tester bonus"
                from_bal, to_bal = await async_transfer(bank_id, discord_id, 50, reason_text)

                # refresh user row for accurate display
                row = get_user_row(discord_id)
                balance_amount = row.get("balance", balance_amount) if row else balance_amount

                # Transaction slip: format similar to /send
                slip_color = get_embed_color_for_id(bank_id)
                slip_embed = discord.Embed(
                    title="Beta-tester Bonus — Transaction Slip",
                    color=discord.Color(slip_color),
                    description=(
                        f"**{username}** received **$50** from the Bank as a beta-tester bonus.\n\n"
                        f"Amount: **$50**\n"
                        f"Bank remaining balance: **${fmt_commas(from_bal)}**\n\n"
                        f"Keep in mind your balance may change during the beta!"
                    )
                )
                if pfp:
                    slip_embed.set_thumbnail(url=pfp)
                await safe_respond(ctx, embed=slip_embed)
            except Exception as e:
                # If transfer unexpectedly failed despite bank_balance check, show helpful embed and log
                print("Beta transfer error after bank balance check:", e)
                bank_row = get_user_row(bank_id)
                bank_balance = int(bank_row.get("balance", 0)) if bank_row else 0
                bank_color = get_embed_color_for_id(bank_id)
                bank_embed = discord.Embed(
                    title="Beta-tester bonus Unavailable",
                    color=discord.Color(bank_color),
                    description=(
                        f"The bank could not provide a beta-tester bonus at this time.\n"
                        f"Bank balance: **${fmt_commas(bank_balance)}**\n"
                        f"You can still start using your account normally."
                    )
                )
                await safe_respond(ctx, embed=bank_embed)
        else:
            # bank doesn't have enough to give 50 (or bank not available). Show bank balance and note.
            bank_color = get_embed_color_for_id(bank_id) if bank_id else EMBED_COLOR_YELLOW
            bank_embed = discord.Embed(
                title="Beta-tester bonus Unavailable",
                color=discord.Color(bank_color),
                description=(
                    f"The bank could not provide a beta-tester bonus at this time.\n"
                    f"Bank balance: **${fmt_commas(bank_balance)}**\n"
                    f"You can still start using your account normally."
                )
            )
            await safe_respond(ctx, embed=bank_embed)


@bot.slash_command(name="balance", description="Check someone's balance")
async def balance(ctx, user: discord.Option(discord.User, "Person to check", required=False)):
    used_followup = await safe_defer(ctx)
    target = user or ctx.author
    row = get_user_row(str(target.id))
    if not row:
        await safe_respond(ctx, f"{target.display_name} doesn't have an account! Use /register")
        return

    balance_amount = row.get("balance", 0)
    pfp = row.get("pfp")
    color_int = get_embed_color_for_id(str(target.id))

    embed = discord.Embed(
        title=target.display_name,
        color=discord.Color(color_int),
        description=f"**Balance:** ${fmt_commas(balance_amount)}"
    )
    if pfp:
        embed.set_thumbnail(url=pfp)

    if used_followup:
        await ctx.followup.send(embed=embed)
    else:
        await safe_respond(ctx, embed=embed)


@bot.slash_command(name="send", description="Send money to another user")
async def send(
    ctx,
    recipient: discord.Option(discord.User, "The person to send money to"),
    amount: discord.Option(int, "Amount to send"),
    reason: discord.Option(str, "Reason to send money")
):
    used_followup = await safe_defer(ctx)
    if amount <= 0:
        await safe_respond(ctx, "Amount must be greater than 0.")
        return
    if ctx.author.id == recipient.id:
        await safe_respond(ctx, "You cannot send money to yourself.")
        return

    sender_id = str(ctx.author.id)
    recipient_id = str(recipient.id)

    if not get_user_row(sender_id) or not get_user_row(recipient_id):
        await safe_respond(ctx, "You or the recipient don't have an account! Use /register")
        return

    try:
        from_bal, to_bal = await async_transfer(sender_id, recipient_id, amount, reason)
    except Exception as e:
        err = str(e)
        if "insufficient" in err.lower():
            await safe_respond(ctx, "You don't have enough funds!")
        else:
            await safe_respond(ctx, f"Transaction failed: {err}")
        return

    sender_row = get_user_row(sender_id)
    recipient_row = get_user_row(recipient_id)
    color_int = parse_color_hex_to_int(sender_row.get("color"))
    embed = discord.Embed(
        title="Transaction Complete",
        color=discord.Color(color_int),
        description=(
            f"**{ctx.author.display_name} ({clean(from_bal)})** sent **${fmt_commas(amount)}** to "
            f"**{recipient.display_name} ({clean(to_bal)})**\n{reason}"
        )
    )
    thumb = recipient_row.get("pfp")
    if thumb:
        embed.set_thumbnail(url=thumb)

    if used_followup:
        await ctx.followup.send(embed=embed)
    else:
        await safe_respond(ctx, embed=embed)


@bot.slash_command(name="request", description="Request someone to send you money")
async def request(
    ctx,
    target: discord.Option(discord.User, "Person who should send the money"),
    amount: discord.Option(int, "Amount requested"),
    reason: discord.Option(str, "Reason")
):
    used_followup = await safe_defer(ctx)
    if target.id == ctx.author.id:
        await safe_respond(ctx, "You cannot request money from yourself.")
        return

    requester_row = get_user_row(str(ctx.author.id))
    requester_bal = requester_row.get("balance", 0) if requester_row else 0
    target_row = get_user_row(str(target.id))
    target_bal = target_row.get("balance", 0) if target_row else 0

    embed = discord.Embed(
        title="Request to Send Money",
        color=discord.Color(get_embed_color_for_id(str(ctx.author.id))),
        description=(
            f"**{ctx.author.display_name} ({clean(requester_bal)})** is requesting **${fmt_commas(amount)}** "
            f"from **{target.display_name} ({clean(target_bal)})**\n{reason}"
        )
    )
    embed.set_thumbnail(url=ctx.author.display_avatar.url)

    view = RequestSendButton(requester=ctx.author, recipient=target, amount=amount, reason=reason)

    if used_followup:
        await ctx.followup.send(embed=embed, view=view)
    else:
        await safe_respond(ctx, embed=embed, view=view)


@bot.slash_command(name="color", description="Set your color (hex code, e.g., #FF00FF)")
async def color(ctx, hex_code: discord.Option(str, "Hex color code, like #FF00FF")):
    if not (hex_code.startswith("#") and len(hex_code) == 7 and all(c in "0123456789ABCDEFabcdef" for c in hex_code[1:])):
        await safe_respond(ctx, "Invalid hex code. Example: `#FF00FF`", ephemeral=True)
        return

    discord_id = str(ctx.author.id)
    if not get_user_row(discord_id):
        await safe_respond(ctx, "You don't have an account yet! Use /register first.", ephemeral=True)
        return

    supabase.table("users").update({"color": hex_code}).eq("discord_id", discord_id).execute()
    await safe_respond(ctx, f"Your color has been updated to `{hex_code}`!")


@bot.slash_command(name="help", description="Show all available commands and info about the bot")
async def help_command(ctx):
    used_followup = await safe_defer(ctx, ephemeral=True)

    # Use the bot's stored user color for help embed, falling back to constant
    bot_id_str = str(bot.user.id) if bot.user else None
    color_int = get_embed_color_for_id(bot_id_str) if bot_id_str else EMBED_COLOR_YELLOW

    embed = discord.Embed(
        title="FlipCoin",
        color=discord.Color(color_int),
        description=(
            "Welcome to **FlipCoin**\n\n"
            "FlipCoin is a legitimate centralized digital currency\n\n"
            "It's purposefully untethered from the USD or computing power in order to create a freer market.\n\n"
        )
    )

    commands_info = [
        ("/register", "Create or update your account"),
        ("/balance [user]", "Check your balance or someone else's."),
        ("/send <recipient> <amount> <reason>", "Send money to another user."),
        ("/request <user> <amount> <reason>", "Request money from another user."),
        ("/color <hex>", "Set your color for your account in Hex, e.g., #FF00FF."),
        ("/help", "Show info about FlipCoin and all bot commands.")
    ]

    for cmd, desc in commands_info:
        embed.add_field(name=cmd, value=desc, inline=False)

    embed.set_footer(text="All transactions are final. Use /register before interacting.")

    if used_followup:
        await ctx.followup.send(embed=embed, ephemeral=True)
    else:
        await safe_respond(ctx, embed=embed, ephemeral=True)


# ---------------- run ----------------
if __name__ == "__main__":
    bot.run(TOKEN)
