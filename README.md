# MAG CAMP - Phase 2

ترقية مباشرة فوق المرحلة الأولى. الترحيل Additive-only ولا يحذف أو يعيد إنشاء جداول users / rooms / workers / assignments.

## النشر
ارفع جميع الملفات إلى جذر GitHub. Render سيعيد النشر تلقائياً.

## ملاحظات حماية القاعدة
- `ensure_schema()` يستخدم `CREATE TABLE IF NOT EXISTS` و`ALTER TABLE ADD COLUMN` فقط.
- لا يوجد `DROP TABLE` أو حذف للبيانات.
- قاعدة المرحلة الأولى المرفقة محفوظة كما هي مع إضافة جداول المرحلة الثانية عند الحاجة.
