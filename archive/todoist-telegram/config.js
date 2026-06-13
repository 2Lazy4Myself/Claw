// Central configuration: project IDs, section names, schedules, and Telegram credentials.
// All secrets are loaded from environment variables via .env (never hardcoded).

require("dotenv").config();

const config = {
  projects: {
    work: {
      id: process.env.WORK_PROJECT_ID,
      label: "Work",
      emoji: "💼",
      schedules: {
        digest: "07:30",
        nags: ["07:45", "12:00", "16:00"],
        evening: null
      }
    },
    home: {
      id: process.env.HOME_PROJECT_ID,
      label: "Home",
      emoji: "🏠",
      schedules: {
        digest: "07:15",
        nags: ["07:15"],
        evening: "18:00"
      }
    }
  },
  sections: {
    TODAY: "Today",
    WAITING: "Waiting For",
    UNPROCESSED: "Unprocessed",
    NEXT_FEW: "Next 2-3 Days",
    THIS_WEEK: "This Week",
    NEXT_WEEK: "Next Week"
  },
  telegram: {
    botToken: process.env.TELEGRAM_BOT_TOKEN,
    chatId: process.env.TELEGRAM_CHAT_ID
  },
  STALENESS_CONFIG: {
    // How long a task can sit in each section before being flagged (in days)
    thresholds: {
      'Next 2-3 Days': 4,
      'This Week': 8,
      'Next Week': 14,
      'This Month': 30,
    },
    // Soft warning threshold for This Month (days) - shown differently
    softWarnThreshold: {
      'This Month': 21,
    },
    // Section IDs that are monitored for staleness - both projects
    monitoredSections: {
      work: {
        'Next 2-3 Days': '6Rmj8jq5vMwhrmfW',
        'This Week':     '6Rmj8mJVcR9JhRpW',
        'Next Week':     '6V63Gr28FH8H2q54',
        'This Month':    '6V63GxJh2mFj3MX4',
      },
      home: {
        'Next 2-3 Days': '6c39w73rGgjv965r',
        'This Week':     '6gXj6JCHxM57pxxr',
        'Next Week':     '6gXj6JJ9hFcQCH9J',
        'This Month':    '6gXj6JPmV8xmcHVJ',
      },
    },
    // If a task has a due_date >= today, skip staleness flagging for it
    skipIfFutureDueDate: true,
  },

  WEEKLY_CALLOUT_SCHEDULE: '30 16 * * 5',  // Friday 16:30
};

module.exports = config;
