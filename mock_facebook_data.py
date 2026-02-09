"""
Mock Facebook data for testing multi-source UI
"""
from datetime import datetime

MOCK_FACEBOOK_POSTS = [
    {
        "post_id": "fb_mock_1",
        "post_url": "https://facebook.com/groups/mock/posts/1",
        "text": "דירה להשכרה ברמת אביב! 3 חדרים, 75 מ״ר, 5,500 ₪ לחודש. קומה 2, מעלית, חניה.",
        "time": datetime(2026, 2, 6, 10, 0, 0),
    },
    {
        "post_id": "fb_mock_2",
        "post_url": "https://facebook.com/groups/mock/posts/2",
        "text": "להשכרה בפלורנטין 2 חד׳ 50 מ״ר 4,200 ₪ משופצת ומאובזרת",
        "time": datetime(2026, 2, 6, 9, 30, 0),
    },
    {
        "post_id": "fb_mock_3",
        "post_url": "https://facebook.com/groups/mock/posts/3",
        "text": "תל אביב צפון - 4 חדרים 95 מ״ר 7,000 ₪ קומה 3 עם מרפסת גדולה",
        "time": datetime(2026, 2, 6, 8, 15, 0),
    },
    {
        "post_id": "fb_mock_4",
        "post_url": "https://facebook.com/groups/mock/posts/4",
        "text": "דירת 3.5 חדרים ברמת החייל 80 מ״ר 6,200 ₪ כניסה מיידית",
        "time": datetime(2026, 2, 5, 16, 20, 0),
    },
    {
        "post_id": "fb_mock_5",
        "post_url": "https://facebook.com/groups/mock/posts/5",
        "text": "להשכרה נווה צדק! 2 חדרים, 60 מ״ר, 5,800 ₪ קומת קרקע עם חצר",
        "time": datetime(2026, 2, 5, 14, 45, 0),
    },
]


def get_mock_facebook_data():
    """Return mock Facebook posts for testing"""
    return MOCK_FACEBOOK_POSTS
