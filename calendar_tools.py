import subprocess
from datetime import date, datetime, timedelta

KONSOLEKALENDAR = "/usr/bin/konsolekalendar"


def tool_query_calendars(days=7):
    try:
        days = int(days)
    except (TypeError, ValueError):
        return {"success": False, "error": "Invalid number of days."}

    if not 1 <= days <= 31:
        return {
            "success": False,
            "error": "Days must be between 1 and 31."
        }

    today = date.today()
    end = today + timedelta(days=days - 1)

    try:
        result = subprocess.run(
            [
                KONSOLEKALENDAR,
                "--view",
                "--date", today.isoformat(),
                "--time", datetime.now().strftime("%H:%M"),
                "--end-date", end.isoformat(),
                "--end-time", "23:59"
            ],
            capture_output=True,
            text=True,
            timeout=20
        )

        if result.returncode != 0:
            return {
                "success": False,
                "error": (result.stderr or "Calendar query failed.")[:1500]
            }

        events = (result.stdout or "").strip()

        return {
            "success": True,
            "periodo_inicio": today.isoformat(),
            "periodo_fim": end.isoformat(),
            "events": events[:12000] if events else
                "No events found in the requested period."
        }

    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "error": "Calendar query timed out."
        }

    except Exception as e:
        return {"success": False, "error": str(e)}
