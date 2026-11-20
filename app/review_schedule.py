"""Optional FSRS-4.5 scheduling for the application's binary grading."""
from datetime import datetime, timezone
from math import exp, expm1
import os


MIN_GRADED_ATTEMPTS = 2000
MIN_STABILITY = 0.01
MAX_INTERVAL_DAYS = 36500


def _as_utc(value: str | datetime | None) -> datetime | None:
    """把库里/调用方的时间统一成带时区的 UTC。

    旧行可能是无时区的 ISO（如 ``2026-01-01T00:00:00``）；与 aware 的 now 直接相减
    会抛 TypeError。这里把无时区一律视为 UTC，无法解析则回到「首次复习」。
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def fsrs_enabled(con) -> bool:
    # Counts actual graded submissions, including verified AI grades. A pending
    # submission, a manual mastery edit, or an unverified grade is not a review.
    return os.environ.get("STUDY_REVIEW_SCHEDULER") == "fsrs-lite" and con.execute(
        "SELECT 1 FROM attempts WHERE result IN ('correct','incorrect') "
        "AND grade_source IN ('reference','ai_cross_verified') LIMIT 1 OFFSET ?",
        (MIN_GRADED_ATTEMPTS - 1,),
    ).fetchone() is not None


def fsrs_review(
    correct: bool,
    stability: float | None,
    difficulty: float | None,
    last_review_at: str | None,
    now: datetime,
) -> tuple[float, float, float]:
    """Apply the Again/Good subset of FSRS-4.5 at 90% target retention."""
    # https://github.com/open-spaced-repetition/fsrs4anki/wiki/The-Algorithm/48fe8df71b95a2ed320de8256b5f96697486d7f7
    rating = 3 if correct else 1
    initial_good_difficulty = 5.1618
    now = _as_utc(now) or datetime.now(timezone.utc)
    last_review = _as_utc(last_review_at)
    if stability is None or difficulty is None or last_review is None:
        # A legacy wrongbook row starts its memory state on its first FSRS review.
        next_stability = 3.7145 if correct else 0.4872
        difficulty = initial_good_difficulty - (rating - 3) * 1.2298
    else:
        elapsed_days = max(0.0, (now - last_review).total_seconds() / 86400)
        recall = (1 + (19 / 81) * elapsed_days / stability) ** -0.5
        if correct:
            increase = exp(1.6474) * (11 - difficulty) * stability ** -0.1367 * expm1(1.0461 * (1 - recall))
            next_stability = stability * (1 + increase)
        else:
            after_lapse = 2.1072 * difficulty ** -0.0793 * ((stability + 1) ** 0.3246 - 1) * exp(1.587 * (1 - recall))
            next_stability = min(stability, after_lapse)
        rated_difficulty = difficulty - 0.8975 * (rating - 3)
        difficulty = 0.031 * initial_good_difficulty + 0.969 * rated_difficulty
    stability = min(MAX_INTERVAL_DAYS, max(MIN_STABILITY, next_stability))
    difficulty = min(10.0, max(1.0, difficulty))
    # Stability is itself the interval for target retention 0.90. The app
    # schedules a one-day retry after every incorrect answer.
    interval = float(max(1, round(stability))) if correct else 1.0
    return interval, stability, difficulty
