"""
Generate Telegram session string for Railway deployment.
Run this locally, then paste the output into Railway environment variables.
"""

import asyncio
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError

# Your credentials
API_ID = 30546814
API_HASH = "fc28c35e3b8b87fb1c5e5268d3fdd940"
PHONE = "+46721813503"

async def main():
    print("=" * 60)
    print("Telegram Session String Generator")
    print("=" * 60)
    print(f"\nPhone: {PHONE}")
    print("\nStep 1: Sending code request to Telegram...")
    
    client = TelegramClient(StringSession(), API_ID, API_HASH)
    await client.connect()
    
    # Send code
    await client.send_code_request(PHONE)
    print("✓ Code sent! Check your Telegram app/SMS.")
    
    # Get code from user
    code = input("\nStep 2: Enter the code you received: ").strip()
    
    try:
        await client.sign_in(PHONE, code)
        print("✓ Signed in successfully!")
    except SessionPasswordNeededError:
        password = input("\nTwo-factor auth enabled. Enter your password: ").strip()
        await client.sign_in(password=password)
        print("✓ Signed in with 2FA!")
    
    # Save session
    session_string = client.session.save()
    
    print("\n" + "=" * 60)
    print("SUCCESS! COPY THIS TO RAILWAY:")
    print("=" * 60)
    print(f"\nTELEGRAM_SESSION_STRING={session_string}\n")
    print("=" * 60)
    print("\nInstructions:")
    print("1. Copy the line above (starting with 1AQA...)")
    print("2. Go to Railway → Your Project → Variables")
    print("3. Replace the old TELEGRAM_SESSION_STRING value")
    print("4. Click 'Redeploy' to restart the bot")
    print("\n⚠️  DO NOT log out of Telegram on your phone or")
    print("    you'll need to regenerate this again!")
    
    await client.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
