# GovMap API Registration - Email Template

## Contact Information

**Survey of Israel - GovMap Support:**
- **Email**: service@mapi.gov.il
- **Phone**: 03-9298853 / 6274
- **Website**: [Survey of Israel](https://www.gov.il/en/departments/survey_of_israel)
- **GovMap Site**: [govmap.gov.il](https://www.govmap.gov.il/?lang=en)

---

## Email Template (English)

**To**: service@mapi.gov.il
**Subject**: GovMap API Access Request - Real Estate Application

```
Dear Survey of Israel / GovMap Support Team,

I am writing to request API access to the GovMap service for a real estate
market analysis application.

APPLICATION DETAILS:
- Application Name: Yad 1.5 - Smart Real Estate Search
- Purpose: Real estate market analysis and property transaction research
- Use Case: Analyzing historical real estate transactions to provide market
  insights to potential buyers and renters in Israel

SPECIFIC DATA NEEDS:
- Real estate transaction data (Layer 16)
- Geocoding services for Israeli addresses
- Property location and neighborhood information

TECHNICAL REQUIREMENTS:
- API Token for programmatic access
- REST API documentation
- Rate limits and usage guidelines

CONTACT INFORMATION:
- Name: [Your Name]
- Organization: [Your Organization/Personal]
- Email: [Your Email]
- Phone: [Your Phone Number]
- Website: [If applicable]

The application will help Israeli citizens make informed decisions about
real estate transactions by providing transparent access to historical
market data.

Thank you for your consideration. I look forward to hearing from you.

Best regards,
[Your Name]
```

---

## Email Template (Hebrew) - תבנית מייל בעברית

**אל**: service@mapi.gov.il
**נושא**: בקשה לגישה ל-API של GovMap - אפליקציית נדל"ן

```
שלום רב,

אני פונה אליכם בבקשה לקבל גישה ל-API של שירות GovMap למטרת פיתוח
אפליקציה לניתוח שוק הנדל"ן בישראל.

פרטי האפליקציה:
- שם האפליקציה: Yad 1.5 - חיפוש נדל"ן חכם
- מטרה: ניתוח שוק הנדל"ן ומחקר עסקאות נכסים
- תיאור השימוש: ניתוח עסקאות נדל"ן היסטוריות כדי לספק תובנות שוק
  לקונים ושוכרים פוטנציאליים בישראל

נתונים נדרשים:
- נתוני עסקאות נדל"ן (שכבה 16)
- שירותי גיאוקודינג לכתובות ישראליות
- מידע על מיקום נכסים ושכונות

דרישות טכניות:
- API Token לגישה פרוגרמטית
- תיעוד REST API
- מגבלות שימוש והנחיות

פרטי קשר:
- שם: [השם שלך]
- ארגון: [הארגון/אישי]
- אימייל: [האימייל שלך]
- טלפון: [מספר הטלפון שלך]
- אתר: [במידה ורלוונטי]

האפליקציה תסייע לאזרחי ישראל לקבל החלטות מושכלות בנוגע לעסקאות
נדל"ן על ידי מתן גישה שקופה לנתוני שוק היסטוריים.

תודה רבה על תשומת הלב,
[השם שלך]
```

---

## What to Expect

### Response Time
- Typical response: 3-7 business days
- May require follow-up or additional information

### Possible Outcomes

1. **✅ Approved**
   - You'll receive an API token (UUID format)
   - API documentation and guidelines
   - Rate limits and terms of use

2. **📋 Information Request**
   - May ask for more details about your application
   - Might require organizational affiliation
   - Could ask for intended usage volume

3. **🔄 Alternative Offered**
   - May direct you to public datasets on data.gov.il
   - Might suggest alternative access methods
   - Could provide limited access tier

### If No Response After 1 Week

**Follow-up Actions:**
1. Call: 03-9298853 or 6274
2. Try alternative email: apn@mapi.gov.il
3. Visit gov.il contact form: [Survey of Israel Contact](https://www.gov.il/en/departments/survey_of_israel/govil-landing-page)

---

## While Waiting for Approval

Your application is **fully functional** with sample data:

```bash
# Start the server
cd /home/doronbrooder/app
python server.py

# Test Deep Analysis with sample data
# Navigate to: http://localhost:8181
# Switch to "ניתוח מעמיק" mode
# Try: "שכונת לב העיר, תל אביב" with 3 rooms
```

Once you receive the token:
```bash
export GOVMAP_API_TOKEN="your-token-here"
python server.py
```

---

## Additional Resources

- [GovMap Official Site](https://www.govmap.gov.il/?lang=en)
- [Survey of Israel](https://www.gov.il/en/departments/survey_of_israel)
- [Bellingcat GovMap Guide](https://bellingcat.gitbook.io/toolkit/more/all-tools/govmap)
- [GovMap GitHub Examples](https://github.com/yovavsanders/govmap)

---

**Next Steps:**
1. ✉️ Fill in your details in the template above
2. 📤 Send email to service@mapi.gov.il
3. ⏳ Wait 3-7 days for response
4. 📞 Follow up if needed
5. 🎉 Configure token when received

Good luck with your registration! 🚀
