{% extends "base.html" %}{% block content %}
<div class="panel form"><h2>جولة الغرفة {{room_no}}</h2><form method="post"><label>المسجلون بالنظام</label><input value="{{registered}}" readonly>
<label>العدد الفعلي</label><input type="number" min="0" name="actual_count" required>
<label>النظافة</label><select name="cleanliness"><option>ممتازة</option><option>مقبولة</option><option>سيئة</option></select>
<label>الأرقام الوظيفية للزائدين</label><input name="extra_employee_nos" placeholder="مثال: 12345, 67890">
<label>ملاحظات</label><textarea name="notes"></textarea><button>حفظ الجولة</button></form></div>
{% endblock %}