import sqlite3
from collections import namedtuple
from datetime import datetime

from dateutil import parser
import praw
import requests
from praw.models import Comment

from conf import *

# Global
target_subreddits = ["discordstatus"]

# Connect to db
conn = sqlite3.connect("bot.db")
c = conn.cursor()

# Create tables
c.execute("CREATE TABLE IF NOT EXISTS incidents (status_id text, last_update text)")
c.execute("CREATE TABLE IF NOT EXISTS posts (status_id text, reddit_id text)")
c.execute("CREATE TABLE IF NOT EXISTS updates (status_id text, update_id text)")


# Connect to reddit
def reddit():
    # Create the Reddit instance
    user_agent = ("Discord Status 0.1")
    return praw.Reddit(client_id=REDDIT_CLIENT,
                       client_secret=REDDIT_SECRET,
                       username=REDDIT_USERNAME,
                       password=REDDIT_PASS,
                       user_agent=user_agent)


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
    # Generate post
    with open("templates/new.md", "r") as f:
        post = f.read()
    date = date_iso(incident["created_at"])
    title = "{} - Discord Service Interruption - {}".format(incident["name"], date)
    summary = ""
    if incident["incident_updates"]:
        summary = "\n{}\n".format(update_format(incident["incident_updates"][-1]))
    post = post.format(title=title, incident=namedtuple("Incident", incident.keys())(*incident.values()),
                       date=date, incident_summary=summary)

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


# Look for changed incidents
def incident_check():
    # Fetch known incidents from db
    known_incidents = {}
    for row in c.execute("SELECT * FROM incidents"):
        known_incidents[row[0]] = row[1]

    # Fetch incidents from API
    r = requests.get("https://status.discord.com/api/v2/incidents.json")
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
    incident_check()
    mod_check()


# Run and then close db
run()
conn.close()
