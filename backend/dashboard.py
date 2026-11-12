from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from flask import jsonify
from sqlalchemy import func

from backend.constants import DASHBOARD_IGNORED_USER_NAMES
from backend.database import db
from backend.models import StepAnalytics, User

ACTIVE_DAY_MIN_STEPS_EXCLUSIVE = 2
ACTIVATED_USER_MIN_STEPS_EXCLUSIVE = 7
RECENT_ACTIVE_USER_MIN_STEPS_INCLUSIVE = 2
PACIFIC_TIMEZONE = ZoneInfo('America/Los_Angeles')


def _as_pacific(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(PACIFIC_TIMEZONE)


def dashboard_api():
    ignored_user_names = {name.strip().lower() for name in DASHBOARD_IGNORED_USER_NAMES if name and name.strip()}
    now_pacific = datetime.now(timezone.utc).astimezone(PACIFIC_TIMEZONE)
    recent_activity_cutoff = now_pacific - timedelta(days=7)
    activity_rows = (
        db.session.query(
            StepAnalytics.userId.label('user_id'),
            User.email.label('email'),
            StepAnalytics.date.label('event_date'),
            StepAnalytics.draftId.label('draft_id'),
        )
        .outerjoin(User, StepAnalytics.userId == User.id)
        .order_by(StepAnalytics.date.asc(), StepAnalytics.id.asc())
        .all()
    )
    activity_counts_by_user_day = {}
    timeline_by_day = {}

    for row in activity_rows:
        email = row.email or f'User {row.user_id}'
        if email.lower() in ignored_user_names or row.event_date is None:
            continue

        event_date_pacific = _as_pacific(row.event_date)
        day_key = event_date_pacific.date().isoformat()
        user_day_key = (row.user_id, day_key)
        activity_counts_by_user_day[user_day_key] = activity_counts_by_user_day.get(user_day_key, 0) + 1

        if day_key not in timeline_by_day:
            timeline_by_day[day_key] = {
                'stepCount': 0,
                'draftIds': set(),
            }

        timeline_by_day[day_key]['stepCount'] += 1
        if row.draft_id not in (None, 0):
            timeline_by_day[day_key]['draftIds'].add(row.draft_id)

    active_day_rows = [
        key for key, count in activity_counts_by_user_day.items()
        if count > ACTIVE_DAY_MIN_STEPS_EXCLUSIVE
    ]
    active_days_by_user = {}

    for user_id, _day_key in active_day_rows:
        active_days_by_user[user_id] = active_days_by_user.get(user_id, 0) + 1

    activated_user_ids = set()
    active_user_ids = set()

    for (user_id, day_key), count in activity_counts_by_user_day.items():
        day_value = datetime.fromisoformat(day_key).date()
        if count > ACTIVATED_USER_MIN_STEPS_EXCLUSIVE:
            activated_user_ids.add(user_id)
        if count >= RECENT_ACTIVE_USER_MIN_STEPS_INCLUSIVE and day_value >= recent_activity_cutoff.date():
            active_user_ids.add(user_id)

    usage_rows = (
        db.session.query(
            StepAnalytics.userId.label('user_id'),
            User.email.label('email'),
            func.count(StepAnalytics.id).label('total_steps'),
            func.count(func.distinct(StepAnalytics.draftId)).label('drafts_created'),
            func.min(StepAnalytics.date).label('first_usage'),
            func.max(StepAnalytics.date).label('last_usage'),
        )
        .outerjoin(User, StepAnalytics.userId == User.id)
        .group_by(StepAnalytics.userId, User.email)
        .order_by(func.count(StepAnalytics.id).desc(), User.email.asc())
        .all()
    )
    timeline_rows = (
        db.session.query(
            StepAnalytics.date.label('event_date'),
            StepAnalytics.userId.label('user_id'),
            StepAnalytics.draftId.label('draft_id'),
            User.email.label('email'),
        )
        .outerjoin(User, StepAnalytics.userId == User.id)
        .order_by(StepAnalytics.date.asc(), StepAnalytics.id.asc())
        .all()
    )

    users = []
    total_steps = 0
    total_drafts_created = 0

    for row in usage_rows:
        email = row.email or f'User {row.user_id}'

        if email.lower() in ignored_user_names:
            continue

        first_usage = row.first_usage
        last_usage = row.last_usage

        if first_usage is None:
            continue

        first_usage = _as_pacific(first_usage)
        last_usage = _as_pacific(last_usage)

        elapsed_days = max((now_pacific - first_usage).total_seconds() / 86400, 1 / 1440)
        steps_per_day = row.total_steps / elapsed_days
        total_steps += row.total_steps
        total_drafts_created += row.drafts_created

        users.append({
            'userId': row.user_id,
            'email': email,
            'totalSteps': row.total_steps,
            'draftsCreated': row.drafts_created,
            'lastUsage': last_usage.isoformat() if last_usage else None,
            'daysSinceFirstUsage': round(elapsed_days, 2),
            'stepsPerDay': round(steps_per_day, 2),
            'activeDays': active_days_by_user.get(row.user_id, 0),
        })

    cumulative_steps = 0
    cumulative_draft_ids = set()
    timeline = []

    for day_key in sorted(timeline_by_day.keys()):
        day_data = timeline_by_day[day_key]
        cumulative_steps += day_data['stepCount']
        cumulative_draft_ids.update(day_data['draftIds'])
        timeline.append({
            'date': day_key,
            'cumulativeSteps': cumulative_steps,
            'cumulativeDrafts': len(cumulative_draft_ids),
        })

    return jsonify({
        'summary': {
            'activeUsers': len(active_user_ids),
            'activatedUsers': len(activated_user_ids),
            'totalDraftsCreated': total_drafts_created,
            'totalSteps': total_steps,
        },
        'timeline': timeline,
        'users': users,
    })
