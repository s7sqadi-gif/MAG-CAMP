# MAG CAMP — Phase 4.2 Beta

الإصدار الثاني من المرحلة الرابعة، مبني فوق Phase 4.1 دون إعادة بناء النظام.

## أبرز الإضافات

- لوحة مدير السكن والخدمات المساندة: الشواغر ومواقعها، التكدس ومواقعه، الإشغال حسب نطاق الصلاحية.
- لوحة مشرف السكن: تعرض الغرف والشواغر والتكدس التابعة للمشرف فقط.
- لوحة مدير الصيانة: البلاغات الجديدة والمفتوحة وقيد التنفيذ والمغلقة، مع توزيع حسب النوع والزون.
- إرفاق صورة إلزامية عند إنشاء بلاغ صيانة جديد.
- إرفاق صورة إلزامية عند إنشاء بلاغ صيانة من داخل الجولة الأسبوعية.
- سجل صور مرتبط بكل بلاغ صيانة.
- تسجيل مستلزمات التسكين: السرير، المرتبة، المخدة، الشرشف، البطانية.
- شاشة مستلزمات متاحة لمدير السكن والخدمات المساندة والمشرف والمراقب حسب الصلاحيات.
- استمرار شعار مجموعة المجال العربي في جميع الشاشات.

## التشغيل

```bash
pip install -r requirements.txt
python app.py
```

## ملاحظة مهمة للنشر

حافظ على مجلد `uploads` كمساحة تخزين دائمة في بيئة الإنتاج حتى لا تضيع صور البلاغات عند إعادة النشر.

## Enterprise Phase 4.5 additions
- Language selection is shown before sign-in. Every user chooses Arabic or English.
- Existing production data is preserved through additive SQLite migrations.
- New housing requests require profession and mobile number and include housing-kit delivery.
- Historical timelines intentionally start with new system operations; no legacy dates are invented.
- New routes: `/occupancy-management`, `/occupancy-map`, `/notifications`, `/workers/<id>`.
