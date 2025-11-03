"""
Discord Voice Activity Logger Bot (Async Stable Build)

Tracks users joining, leaving, or moving between Discord voice channels.
Each user has their own Google Sheets worksheet (named after their display name).
If the worksheet doesn't exist, it is automatically created.

Features:
- Join → start session
- Leave → end session, log if >= 5 minutes
- Move → treat as leave + join
- Async-safe Google Sheets logging
"""

import os
import sys
import datetime
import json
import discord
from discord.ext import commands
from dotenv import load_dotenv
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from gspread.exceptions import WorksheetNotFound, APIError
import pytz
import asyncio
import traceback

# ==================== Load environment ====================
load_dotenv()

# Validate required environment variables
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")

if not DISCORD_TOKEN:
    print("❌ Error: DISCORD_TOKEN is not set in .env file")
    sys.exit(1)

if not SPREADSHEET_ID:
    print("❌ Error: SPREADSHEET_ID is not set in .env file")
    sys.exit(1)

try:
    MIN_SESSION_MINUTES = int(os.getenv("MIN_SESSION_MINUTES", "5"))
    if MIN_SESSION_MINUTES < 0:
        raise ValueError("MIN_SESSION_MINUTES must be >= 0")
except ValueError as e:
    print(f"❌ Error: Invalid MIN_SESSION_MINUTES value: {e}")
    sys.exit(1)

SESSION_BACKUP_FILE = "active_sessions.json"

# ==================== Google Sheets setup ====================
SCOPE = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
GCLIENT = None
SPREADSHEET = None

def initialize_google_sheets():
    """Initialize Google Sheets client with error handling"""
    global GCLIENT, SPREADSHEET
    try:
        if not os.path.exists("credentials.json"):
            print("❌ Error: credentials.json file not found")
            sys.exit(1)

        credentials = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", SCOPE)
        GCLIENT = gspread.authorize(credentials)
        SPREADSHEET = GCLIENT.open_by_key(SPREADSHEET_ID)
        print("✅ Google Sheets initialized successfully")
        return True
    except FileNotFoundError:
        print("❌ Error: credentials.json file not found")
        sys.exit(1)
    except Exception as e:
        print(f"❌ Error initializing Google Sheets: {e}")
        traceback.print_exc()
        sys.exit(1)

def setup_summary_row_if_needed(worksheet):
    """
    Check if summary row (A2) needs formula and add it if empty.
    Assumes A2:D2 is already merged.
    """
    try:
        # Check if A2 is empty
        a2_value = worksheet.acell('A2').value

        if not a2_value or a2_value.strip() == "":
            # Add the total time formula
            # Use raw string without escaping quotes
            total_formula = '=IFERROR(LET(times, FILTER(D3:D1000, D3:D1000<>"", D3:D1000<>"작업 중···"), hours, ARRAYFORMULA(VALUE(REGEXEXTRACT(times, "(\\d+)H"))), minutes, ARRAYFORMULA(VALUE(REGEXEXTRACT(times, "(\\d+)M"))), totalMinutes, SUM(hours * 60 + minutes), finalHours, INT(totalMinutes / 60), finalMinutes, MOD(totalMinutes, 60), "작업시간 총합 : " & finalHours & "시간 " & finalMinutes & "분"), "작업시간 총합 : 0시간 0분")'

            # Use new API with USER_ENTERED to make formulas work
            worksheet.update(values=[[total_formula]], range_name='A2', value_input_option='USER_ENTERED')
            print(f"   ✅ Added summary formula to {worksheet.title}")
            return True
        else:
            print(f"   ℹ️ Summary row already exists in {worksheet.title}")
            return False
    except Exception as e:
        print(f"   ⚠️ Error setting up summary row for {worksheet.title}: {e}")
        return False

# ==================== Discord setup ====================
intents = discord.Intents.default()
intents.voice_states = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ==================== Config ====================
TRACKED_USERS = {
    "송": pytz.timezone("America/Toronto"),
    "휼": pytz.timezone("Asia/Seoul"),
    "취향의이데아": pytz.timezone("Asia/Seoul")
}
IGNORED_CHANNEL = "휴게실"

# Validate configuration
if not TRACKED_USERS:
    print("❌ Error: TRACKED_USERS is empty. No users will be tracked.")
    sys.exit(1)

# Active sessions storage: {username: {"join_time": datetime, "channel": str, "row_number": int}}
active_sessions = {}

# ==================== Utility functions ====================
def _write_sessions_sync(data):
    """Internal synchronous function for writing sessions to file"""
    with open(SESSION_BACKUP_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

async def save_sessions():
    """Save active sessions to file with error handling (async to avoid blocking)"""
    try:
        data = {}
        for k, v in active_sessions.items():
            join_time = v["join_time"]
            data[k] = {
                "join_time": join_time.isoformat(),
                "channel": v["channel"],
                "row_number": v.get("row_number"),
                "timezone": str(join_time.tzinfo) if join_time.tzinfo else None
            }

        # Run file I/O in thread pool to avoid blocking event loop
        await asyncio.to_thread(_write_sessions_sync, data)
    except Exception as e:
        print(f"⚠️ Error saving sessions: {e}")
        traceback.print_exc()

def load_sessions():
    """Load active sessions from file with error handling"""
    global active_sessions
    if not os.path.exists(SESSION_BACKUP_FILE):
        return

    try:
        with open(SESSION_BACKUP_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        for username, info in data.items():
            try:
                join_time = datetime.datetime.fromisoformat(info["join_time"])

                # Restore timezone if it was stored
                if "timezone" in info and info["timezone"] and username in TRACKED_USERS:
                    tz = TRACKED_USERS[username]
                    # If the loaded datetime is naive, localize it
                    if join_time.tzinfo is None:
                        join_time = tz.localize(join_time)

                active_sessions[username] = {
                    "join_time": join_time,
                    "channel": info["channel"],
                    "row_number": info.get("row_number")
                }
            except Exception as e:
                print(f"⚠️ Error loading session for {username}: {e}")
                continue

        print(f"📂 Loaded {len(active_sessions)} active session(s)")
    except json.JSONDecodeError as e:
        print(f"⚠️ Error parsing {SESSION_BACKUP_FILE}: {e}")
        print(f"⚠️ Starting with empty sessions")
    except Exception as e:
        print(f"⚠️ Error loading sessions: {e}")
        traceback.print_exc()

def format_duration(delta: datetime.timedelta) -> str:
    """
    Format a timedelta as 'XH YYM' (e.g., '2H 30M').

    Args:
        delta: Time duration to format

    Returns:
        Formatted string in 'XH YYM' format
    """
    total_minutes = int(delta.total_seconds() // 60)
    hours, minutes = divmod(total_minutes, 60)
    return f"{hours}H {minutes:02d}M"

def get_or_create_worksheet(username: str):
    """
    Get or create a worksheet for a user (synchronous - call via asyncio.to_thread).

    Args:
        username: User's display name to use as worksheet title

    Returns:
        gspread.Worksheet object

    Raises:
        Exception: If worksheet cannot be created or accessed
    """
    try:
        worksheet = SPREADSHEET.worksheet(username)
    except WorksheetNotFound:
        worksheet = SPREADSHEET.add_worksheet(title=username, rows="1000", cols="4")

        # Set header row
        worksheet.update(values=[["날짜", "시작 시각", "종료 시각", "총 작업 시간"]], range_name="A1:D1")

        # Merge cells A2:D2 for summary row
        worksheet.merge_cells('A2:D2', merge_type='MERGE_ALL')

        # Add formula to calculate total time in A2
        total_formula = '=IFERROR(LET(times, FILTER(D3:D1000, D3:D1000<>"", D3:D1000<>"작업 중···"), hours, ARRAYFORMULA(VALUE(REGEXEXTRACT(times, "(\\d+)H"))), minutes, ARRAYFORMULA(VALUE(REGEXEXTRACT(times, "(\\d+)M"))), totalMinutes, SUM(hours * 60 + minutes), finalHours, INT(totalMinutes / 60), finalMinutes, MOD(totalMinutes, 60), "작업시간 총합 : " & finalHours & "시간 " & finalMinutes & "분"), "작업시간 총합 : 0시간 0분")'

        worksheet.update(values=[[total_formula]], range_name='A2', value_input_option='USER_ENTERED')

        print(f"🆕 Created new worksheet: {username}")
    except Exception as e:
        print(f"⚠️ Error getting/creating worksheet for {username}: {e}")
        raise
    return worksheet

async def append_to_sheet_with_retry(worksheet, values, max_retries=3, delay=5):
    """
    Append a row to a worksheet with retry logic (async-safe).

    Args:
        worksheet: gspread.Worksheet to append to
        values: List of values to append as a row
        max_retries: Maximum number of retry attempts
        delay: Delay in seconds between retries

    Returns:
        True if successful, False otherwise
    """
    for attempt in range(max_retries):
        try:
            # Run blocking gspread call in thread pool to avoid blocking event loop
            await asyncio.to_thread(worksheet.append_row, values)
            return True
        except APIError as e:
            print(f"⚠️ APIError on attempt {attempt+1}: {e}")
            if attempt < max_retries - 1:
                await asyncio.sleep(delay)
        except Exception as e:
            print(f"⚠️ Error on attempt {attempt+1}: {e}")
            if attempt < max_retries - 1:
                await asyncio.sleep(delay)
    print(f"❌ Failed to append row after {max_retries} attempts: {values}")
    return False

def get_next_row_number(worksheet):
    """
    Get the next available row number (synchronous).

    Args:
        worksheet: gspread.Worksheet

    Returns:
        Row number for next entry
    """
    return len(worksheet.get_all_values()) + 1

async def log_join_session(username: str, join_time: datetime.datetime, channel_name: str):
    """
    Immediately log join to Google Sheets when user enters voice channel.

    Args:
        username: User's display name
        join_time: When the user joined
        channel_name: Name of the voice channel

    Returns:
        Row number where data was written, or None if failed
    """
    try:
        # Run blocking worksheet operation in thread pool
        worksheet = await asyncio.to_thread(get_or_create_worksheet, username)

        date_str = join_time.strftime("%m/%d")
        join_str = join_time.strftime("%H:%M")

        # Get next row number before appending
        row_number = await asyncio.to_thread(get_next_row_number, worksheet)

        # Create formula to calculate duration (will show "작업 중···" when C column is empty)
        duration_formula = f'=IF(OR(B{row_number}="", C{row_number}=""), IF(B{row_number}="", "", "작업 중···"), LET(start, TIMEVALUE(B{row_number}), end, TIMEVALUE(C{row_number}), diff, end - start, hours, INT(diff * 24), minutes, ROUND(MOD(diff * 24, 1) * 60, 0), hours & "H " & TEXT(minutes, "00") & "M"))'

        # Write initial row: [날짜, 시작 시각, "", duration_formula]
        row = [date_str, join_str, "", duration_formula]

        # Append row with formula using retry logic
        success = False
        max_retries = 3
        for attempt in range(max_retries):
            try:
                await asyncio.to_thread(worksheet.append_row, row, value_input_option='USER_ENTERED')
                success = True
                break
            except APIError as e:
                print(f"⚠️ APIError on attempt {attempt+1}: {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(5)
            except Exception as e:
                print(f"⚠️ Error on attempt {attempt+1}: {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(5)
        if success:
            print(f"✅ Logged JOIN for {username} at {join_str} in {channel_name} (row {row_number})")
            return row_number
        else:
            print(f"❌ Failed to log JOIN for {username}")
            return None
    except Exception as e:
        print(f"❌ Error logging JOIN for {username}: {e}")
        traceback.print_exc()
        return None

async def update_leave_session(username: str, join_time: datetime.datetime, leave_time: datetime.datetime, duration: datetime.timedelta, row_number: int):
    """
    Update the existing row with leave time and add formula for duration calculation.
    If the session spans multiple days, split it into separate rows.

    Args:
        username: User's display name
        join_time: When the user joined
        leave_time: When the user left
        duration: Session duration (kept for logging purposes)
        row_number: Row number to update
    """
    try:
        worksheet = await asyncio.to_thread(get_or_create_worksheet, username)

        duration_str = format_duration(duration)

        # Check if the session spans multiple days
        join_date = join_time.date()
        leave_date = leave_time.date()

        if join_date == leave_date:
            # Same day - simple update
            leave_str = leave_time.strftime("%H:%M")
            duration_formula = f'=IF(OR(B{row_number}="", C{row_number}=""), IF(B{row_number}="", "", "작업 중···"), LET(start, TIMEVALUE(B{row_number}), end, TIMEVALUE(C{row_number}), diff, end - start, hours, INT(diff * 24), minutes, ROUND(MOD(diff * 24, 1) * 60, 0), hours & "H " & TEXT(minutes, "00") & "M"))'

            def update_row():
                worksheet.update(values=[[leave_str, duration_formula]], range_name=f"C{row_number}:D{row_number}", value_input_option='USER_ENTERED')

            await asyncio.to_thread(update_row)
            print(f"✅ Updated LEAVE for {username}: {leave_str} ({duration_str}) at row {row_number}")
        else:
            # Multiple days - split into separate rows
            current_date = join_time
            rows_to_add = []

            # First row: join_time ~ 23:59 (update existing row)
            end_of_first_day = current_date.replace(hour=23, minute=59, second=59)
            duration_formula = f'=IF(OR(B{row_number}="", C{row_number}=""), IF(B{row_number}="", "", "작업 중···"), LET(start, TIMEVALUE(B{row_number}), end, TIMEVALUE(C{row_number}), diff, end - start, hours, INT(diff * 24), minutes, ROUND(MOD(diff * 24, 1) * 60, 0), hours & "H " & TEXT(minutes, "00") & "M"))'

            def update_first_row():
                worksheet.update(values=[["23:59", duration_formula]], range_name=f"C{row_number}:D{row_number}", value_input_option='USER_ENTERED')

            await asyncio.to_thread(update_first_row)
            print(f"✅ Updated first day for {username}: {join_time.strftime('%m/%d %H:%M')} ~ 23:59")

            # Move to next day
            current_date = current_date.replace(hour=0, minute=0, second=0, microsecond=0) + datetime.timedelta(days=1)

            # Middle days and last day
            while current_date.date() <= leave_date:
                date_str = current_date.strftime("%m/%d")
                start_str = "00:00"

                if current_date.date() == leave_date:
                    # Last day: 00:00 ~ leave_time
                    end_str = leave_time.strftime("%H:%M")
                else:
                    # Middle day: 00:00 ~ 23:59
                    end_str = "23:59"

                # Get the next row number for formula
                next_row = await asyncio.to_thread(get_next_row_number, worksheet)
                duration_formula = f'=IF(OR(B{next_row}="", C{next_row}=""), IF(B{next_row}="", "", "작업 중···"), LET(start, TIMEVALUE(B{next_row}), end, TIMEVALUE(C{next_row}), diff, end - start, hours, INT(diff * 24), minutes, ROUND(MOD(diff * 24, 1) * 60, 0), hours & "H " & TEXT(minutes, "00") & "M"))'

                row = [date_str, start_str, end_str, duration_formula]

                # Append the row
                max_retries = 3
                success = False
                for attempt in range(max_retries):
                    try:
                        await asyncio.to_thread(worksheet.append_row, row, value_input_option='USER_ENTERED')
                        success = True
                        break
                    except APIError as e:
                        print(f"⚠️ APIError on attempt {attempt+1}: {e}")
                        if attempt < max_retries - 1:
                            await asyncio.sleep(5)
                    except Exception as e:
                        print(f"⚠️ Error on attempt {attempt+1}: {e}")
                        if attempt < max_retries - 1:
                            await asyncio.sleep(5)

                if success:
                    print(f"✅ Added continuation for {username}: {date_str} {start_str} ~ {end_str}")
                else:
                    print(f"❌ Failed to add continuation row for {username}")

                current_date += datetime.timedelta(days=1)

            print(f"✅ Split multi-day session for {username} ({duration_str} total)")

        return True
    except Exception as e:
        print(f"❌ Error updating LEAVE for {username}: {e}")
        traceback.print_exc()
        return False

async def delete_session_row(username: str, row_number: int, duration_minutes: float):
    """
    Delete a row for a short session (< MIN_SESSION_MINUTES).

    Args:
        username: User's display name
        row_number: Row number to delete
        duration_minutes: Session duration in minutes
    """
    try:
        worksheet = await asyncio.to_thread(get_or_create_worksheet, username)

        def delete_row():
            worksheet.delete_rows(row_number)

        await asyncio.to_thread(delete_row)
        print(f"🗑️ Deleted short session for {username} ({duration_minutes:.1f} min) at row {row_number}")
        return True
    except Exception as e:
        print(f"❌ Error deleting row for {username}: {e}")
        traceback.print_exc()
        return False

# ==================== Discord event handling ====================
@bot.event
async def on_ready():
    """Bot startup event handler"""
    initialize_google_sheets()
    load_sessions()

    # Setup summary rows for existing worksheets
    print(f"🔧 Checking existing worksheets for summary row...")
    for username in TRACKED_USERS.keys():
        try:
            worksheet = SPREADSHEET.worksheet(username)
            await asyncio.to_thread(setup_summary_row_if_needed, worksheet)
        except Exception as e:
            print(f"   ⚠️ Worksheet '{username}' not found or error: {e}")

    print(f"\n{'='*60}")
    print(f"🤖 Bot logged in as {bot.user.name} ({bot.user.id})")
    print(f"{'='*60}")
    print(f"📋 Configuration:")
    print(f"   • Tracking {len(TRACKED_USERS)} user(s): {', '.join(TRACKED_USERS.keys())}")
    print(f"   • Min session time: {MIN_SESSION_MINUTES} minute(s)")
    print(f"   • Ignored channel: {IGNORED_CHANNEL}")
    print(f"   • Spreadsheet ID: {SPREADSHEET_ID[:20]}...")
    print(f"{'='*60}\n")

@bot.event
async def on_voice_state_update(member, before, after):
    """
    Handle voice state changes (join, leave, move).

    Args:
        member: Discord member whose voice state changed
        before: Voice state before the change
        after: Voice state after the change
    """
    username = member.display_name

    # Only track configured users, ignore bots
    if username not in TRACKED_USERS or member.bot:
        return

    tz = TRACKED_USERS[username]
    now = datetime.datetime.now(tz)
    before_ch = before.channel.name if before.channel else None
    after_ch = after.channel.name if after.channel else None

    # === JOIN ===
    if before.channel is None and after.channel is not None:
        if after_ch == IGNORED_CHANNEL:
            return

        # Immediately log to sheet
        row_number = await log_join_session(username, now, after_ch)

        # Save session with row number
        active_sessions[username] = {
            "join_time": now,
            "channel": after_ch,
            "row_number": row_number
        }
        print(f"▶️ {username} joined {after_ch} at {now.strftime('%H:%M')}")
        await save_sessions()

    # === LEAVE ===
    elif before.channel is not None and after.channel is None:
        session = active_sessions.pop(username, None)
        if session:
            duration = now - session["join_time"]
            total_minutes = duration.total_seconds() / 60
            row_number = session.get("row_number")

            if row_number:
                if total_minutes >= MIN_SESSION_MINUTES:
                    # Update the row with leave time and duration
                    await update_leave_session(username, session["join_time"], now, duration, row_number)
                else:
                    # Delete the row for short sessions
                    await delete_session_row(username, row_number, total_minutes)
            else:
                print(f"⚠️ No row_number found for {username}'s session")

            print(f"⏹️ {username} left {before_ch} at {now.strftime('%H:%M')}")
        await save_sessions()

    # === MOVE ===
    elif before.channel is not None and after.channel is not None and before.channel.id != after.channel.id:
        session = active_sessions.pop(username, None)

        # End previous session
        if session:
            duration = now - session["join_time"]
            total_minutes = duration.total_seconds() / 60
            row_number = session.get("row_number")

            if row_number:
                if total_minutes >= MIN_SESSION_MINUTES:
                    await update_leave_session(username, session["join_time"], now, duration, row_number)
                else:
                    await delete_session_row(username, row_number, total_minutes)

        # Start new session if not moving to ignored channel
        if after_ch != IGNORED_CHANNEL:
            row_number = await log_join_session(username, now, after_ch)
            active_sessions[username] = {
                "join_time": now,
                "channel": after_ch,
                "row_number": row_number
            }
            print(f"🔁 {username} moved {before_ch} → {after_ch} at {now.strftime('%H:%M')}")

        await save_sessions()

@bot.event
async def on_error(event, *args, **kwargs):
    """
    Global error handler for Discord events.

    Args:
        event: Name of the event that raised the error
        *args: Positional arguments passed to the event
        **kwargs: Keyword arguments passed to the event
    """
    print(f"⚠️ Error in {event}")
    print(f"⚠️ Args: {args}")
    print(f"⚠️ Kwargs: {kwargs}")
    traceback.print_exc()

# ==================== Start bot ====================
if __name__ == "__main__":
    """
    Main entry point.

    Starts the Discord bot. The bot will:
    1. Connect to Discord
    2. Initialize Google Sheets (on_ready)
    3. Load any active sessions from backup file (on_ready)
    4. Begin tracking voice state changes (on_voice_state_update)
    """
    print("🚀 Starting Discord Voice Activity Logger...")
    bot.run(DISCORD_TOKEN)
