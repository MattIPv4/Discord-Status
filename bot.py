import sqlite3
from collections import namedtuple
from datetime import datetime
from time import sleep
from base64 import b64encode

from dateutil import parser
import praw
import requests
from praw.models import Comment

from conf import *

# Global
target_subreddits = ["discordstatus"]
target_channels = ["774652047026290688"]
post_to_reddit = True
post_to_discord = True
update_reddit_icon = False
update_discord_icon = True

# Connect to db
conn = sqlite3.connect("bot.db")
c = conn.cursor()

# Create tables
c.execute("CREATE TABLE IF NOT EXISTS incidents (status_id text, last_update text)")
c.execute("CREATE TABLE IF NOT EXISTS posts (status_id text, reddit_id text)")
c.execute("CREATE TABLE IF NOT EXISTS updates (status_id text, update_id text)")
c.execute("CREATE TABLE IF NOT EXISTS messages (status_id text, discord_id text)")
c.execute("CREATE TABLE IF NOT EXISTS data (key_id text, value text)")


# Connect to reddit
def reddit():
    # Create the Reddit instance
    user_agent = ("Discord Status 0.1")
    return praw.Reddit(client_id=REDDIT_CLIENT,
                       client_secret=REDDIT_SECRET,
                       username=REDDIT_USERNAME,
                       password=REDDIT_PASS,
                       user_agent=user_agent)


# Post a message on Discord
def discord_message(content, channel_id):
    r = requests.post(
        "https://discord.com/api/channels/{}/messages".format(channel_id),
        json={
            "content": content
        },
        headers={
            "Authorization": "Bot {}".format(DISCORD_TOKEN)
        }
    )
    r.raise_for_status()
    return r.json()


# Announce a message on Discord
def discord_crosspost_message(channel_id, message_id):
    r = requests.post(
        "https://discord.com/api/channels/{}/messages/{}/crosspost".format(channel_id, message_id),
        headers={
            "Authorization": "Bot {}".format(DISCORD_TOKEN)
        }
    )
    r.raise_for_status()
    return r.json()


# Format an incident update
def update_format(update):
    with open("templates/update_body.md", "r") as f:
        content = f.read()
    content = content.format(status=update["status"].title(), message=update["body"].replace("\n", "\n> "))
    return content


# Format a datetime to english
def date_datetime(dt):
    return dt.strftime("%d %b %Y %H:%M UTC%z").strip()


# Format a unix timestamp to english
def date_unix(unix):
    return date_datetime(datetime.utcfromtimestamp(unix))


# Format a ISO 8601 date to english
def date_iso(iso):
    return date_datetime(parser.parse(iso))


# Handle an updated incident
def incident_update(incident):
    # Get previously posted updates
    posted_updates = []
    for row in c.execute("SELECT * FROM updates WHERE status_id = ?", [incident["id"]]):
        posted_updates.append(row[1])

    # Fetch template
    with open("templates/update.md", "r") as f:
        content = f.read()

    # Loop over updates in incident
    edits = ""
    for update in sorted(incident["incident_updates"], key=lambda x: x["created_at"]):
        # Ignore old
        if update["id"] in posted_updates:
            continue

        # Format update
        edits += "\n\n" + content.format(date=date_iso(update["created_at"]), update=update_format(update))

        # Update db
        c.execute("INSERT INTO updates VALUES (?,?)", [
            incident["id"],
            update["id"]
        ])
    conn.commit()

    # Update main incident db
    c.execute("UPDATE incidents SET last_update = ? WHERE status_id = ?", [
        incident["updated_at"],
        incident["id"]
    ])
    conn.commit()

    # Only bother if there are edits
    if edits:
        if post_to_reddit:
            # Get reddit posts
            posts = []
            for row in c.execute("SELECT * FROM posts WHERE status_id = ?", [incident["id"]]):
                posts.append(row[1])

            # Edit posts
            r = reddit()
            for post in posts:
                try:
                    post = r.submission(post)
                    post.edit(post.selftext + edits)
                except:
                    continue


# Handle a new incident
def new_incident(incident):
    # Update db
    c.execute("INSERT INTO incidents VALUES (?,?)", [
        incident["id"],
        incident["created_at"]
    ])
    if incident["incident_updates"]:
        c.execute("INSERT INTO updates VALUES (?,?)", [
            incident["id"],
            incident["incident_updates"][-1]["id"]
        ])
    conn.commit()

    # Reddit
    if post_to_reddit:
        # Generate post
        with open("templates/new.md", "r") as f:
            post = f.read()
        date = date_iso(incident["created_at"])
        title = "{} - Discord Service Interruption - {}".format(incident["name"], date)
        summary = ""
        if incident["incident_updates"]:
            summary = "\n{}\n".format(update_format(incident["incident_updates"][-1]))
        post = post.format(title=title, incident=namedtuple("Incident", incident.keys())(*incident.values()),
                           date=date, summary=summary)

        # Send post to reddit
        r = reddit()
        for subreddit in target_subreddits:
            try:
                subr = r.subreddit(subreddit)
                subm = subr.submit(title, selftext=post)
            except:
                continue
            print("https://reddit.com" + subm.permalink)
            # Update db
            c.execute("INSERT INTO posts VALUES (?,?)", [
                incident["id"],
                subm.id
            ])
        conn.commit()

    # Discord
    if post_to_discord:
        # Generate post
        with open("templates/new-discord.md", "r") as f:
            post = f.read()
        date = date_iso(incident["created_at"])
        summary = update_format(incident["incident_updates"][-1]) if incident["incident_updates"] else ""
        post = post.format(incident=namedtuple("Incident", incident.keys())(*incident.values()), date=date,
                           summary=summary)

        # Send post to Discord
        for channel in target_channels:
            try:
                msg = discord_message(post, channel)
                discord_crosspost_message(channel, msg["id"])
            except:
                continue
            # Update db
            c.execute("INSERT INTO messages VALUES (?,?)", [
                incident["id"],
                msg["id"]
            ])
            sleep(1)
        conn.commit()

    # Handle other updates in incident
    incident_update(incident)


# Handle a moderator update
def mod_update(reply, incident_id):
    # Generate edit
    with open("templates/mod.md", "r") as f:
        content = f.read()
    date = date_unix(reply.created_utc)
    content = "\n\n" + content.format(submission=reply, date=date,
                                      update=reply.body[len("?update "):].replace("\n", "\n> "))

    # Get reddit posts
    posts = []
    for row in c.execute("SELECT * FROM posts WHERE status_id = ?", [incident_id]):
        posts.append(row[1])

    # Edit posts
    r = reddit()
    edited = []
    for post in posts:
        try:
            post = r.submission(post)
            post.edit(post.selftext + content)
        except:
            continue
        edited.append("https://reddit.com" + post.permalink)

    # Reply
    try:
        reply.upvote()
    except:
        pass
    try:
        reply.reply("The following posts have been updated with your message:\n\n" + "\n".join(
            [" - " + f for f in edited]))
    except:
        pass


# Look for current status
def status_check():
    # Fetch known status from db
    last_status = None
    for row in c.execute("SELECT `value` FROM `data` WHERE `key_id` = 'status'"):
        last_status = row[0]

    # Fetch incidents from API
    r = requests.get("https://discordstatus.com/api/v2/status.json")
    r = r.json()

    # Don't do anything if same
    if r['status']['indicator'] == last_status:
        return

    # Update the subreddit icon
    if update_reddit_icon:
        rd = reddit()
        for subreddit in target_subreddits:
            try:
                subr = rd.subreddit(subreddit)
                subr.stylesheet.upload_mobile_icon("icons/" + r['status']['indicator'] + ".png")
                rd.post(
                    path="/r/" + subreddit + "/api/upload_sr_img",
                    data={"upload_type": "icon"},
                    files={"file": open("icons/" + r['status']['indicator'] + ".png", "rb")}
                )
            except Exception as e:
                print(e)
                continue

    # Update the Discord icon
    if update_discord_icon:
        try:
            with open("icons/" + r['status']['indicator'] + ".png", "rb") as file:
                b64 = b64encode(file.read())
            resp = requests.patch(
                "https://discord.com/api/users/@me",
                json={
                    "avatar": "data:image/png;base64,{}".format(b64.decode('utf-8'))
                },
                headers={
                    "Authorization": "Bot {}".format(DISCORD_TOKEN)
                }
            )
            resp.raise_for_status()
        except Exception as e:
            print(resp.text)
            print(e)
            pass

    # Update db
    c.execute("DELETE FROM `data` WHERE `key_id` = 'status'")
    c.execute("INSERT INTO `data` VALUES (?,?)", [
        "status",
        r['status']['indicator'],
    ])
    conn.commit()


# Look for changed incidents
def incident_check():
    # Fetch known incidents from db
    known_incidents = {}
    for row in c.execute("SELECT * FROM incidents"):
        known_incidents[row[0]] = row[1]

    # Fetch incidents from API
    r = requests.get("https://discordstatus.com/api/v2/incidents.json")
    r = r.json()
    for incident in r["incidents"]:
        # Is a new incident?
        if incident["id"] not in known_incidents and not incident["resolved_at"]:
            new_incident(incident)
            continue

        # Is an updated incident?
        if incident["id"] in known_incidents and incident["updated_at"] != known_incidents[incident["id"]]:
            incident_update(incident)
            continue


# Look for mod commands
def mod_check():
    if not post_to_reddit:
        return

    # Get reddit posts
    posts = {}
    for row in c.execute("SELECT * FROM posts"):
        posts[row[1]] = row[0]

    # Look at inbox
    r = reddit()
    for item in r.inbox.unread(limit=None):
        # Only care for comments
        if isinstance(item, Comment):
            item.mark_read()

            # Ignore non-incident posts
            if item.submission.id not in posts:
                continue

            # Ignore non-update
            if not item.body.startswith("?update"):
                continue

            # Ignore non-mods
            mods = [f for f in item.subreddit.moderator()]
            if item.author not in mods:
                continue

            # We have a valid mod update request
            mod_update(item, posts[item.submission.id])


# Run all
def run():
    status_check()
    incident_check()
    mod_check()


# Run and then close db
run()
conn.close()
