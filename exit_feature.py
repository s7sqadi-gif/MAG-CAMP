import io, json
from contextlib import closing
from datetime import datetime
from flask import abort, redirect, request, url_for, send_file
from openpyxl import Workbook

EXIT_TYPES={'final_exit':'خروج نهائي','left_not_returned':'خرج ولم يعد','absconding':'بلاغ هروب','deceased':'وفاة','project_transfer':'نقل إلى مشروع آخر','resignation':'استقالة','termination':'إنهاء خدمات','other':'أخرى'}

def init_exit_feature(app, conn, now, current_user, login_required, is_admin, page, audit, saudi_today):
    with closing(conn()) as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS worker_exit_requests(id INTEGER PRIMARY KEY AUTOINCREMENT,request_no TEXT NOT NULL UNIQUE,worker_id INTEGER NOT NULL,employee_no TEXT,full_name TEXT,iqama_no TEXT,nationality TEXT,profession TEXT,shift_name TEXT,rest_day TEXT,zone TEXT,room_no TEXT,exit_type TEXT NOT NULL,reason TEXT,notes TEXT,requested_by INTEGER NOT NULL,housing_manager_status TEXT DEFAULT 'pending',housing_manager_by INTEGER,housing_manager_at TEXT,services_manager_status TEXT DEFAULT 'pending',services_manager_by INTEGER,services_manager_at TEXT,final_status TEXT DEFAULT 'pending_housing_manager',created_at TEXT NOT NULL,completed_at TEXT);
        CREATE TABLE IF NOT EXISTS worker_exit_archive(id INTEGER PRIMARY KEY AUTOINCREMENT,source_worker_id INTEGER,employee_no TEXT,full_name TEXT,iqama_no TEXT,nationality TEXT,profession TEXT,phone TEXT,shift_name TEXT,rest_day TEXT,department TEXT,zone TEXT,last_room_no TEXT,exit_type TEXT NOT NULL,reason TEXT,notes TEXT,request_no TEXT,requested_by INTEGER,housing_manager_by INTEGER,services_manager_by INTEGER,created_at TEXT,approved_at TEXT);
        CREATE INDEX IF NOT EXISTS idx_worker_exit_requests_status ON worker_exit_requests(final_status,created_at);
        CREATE INDEX IF NOT EXISTS idx_worker_exit_archive_type ON worker_exit_archive(exit_type,approved_at);
        ''');c.commit()

    @app.get('/worker-exit-requests')
    @login_required
    def worker_exit_requests():
        u=current_user()
        allowed=('housing_supervisor','housing_monitor','data_entry','housing_manager','services_manager','super_admin')
        if u['role'] not in allowed:abort(403)
        with closing(conn()) as c:
            if is_admin(u): rows=c.execute('SELECT x.*,ru.display_name requester FROM worker_exit_requests x LEFT JOIN users ru ON ru.id=x.requested_by ORDER BY x.id DESC LIMIT 500').fetchall()
            else: rows=c.execute('SELECT x.*,ru.display_name requester FROM worker_exit_requests x LEFT JOIN users ru ON ru.id=x.requested_by WHERE x.requested_by=? ORDER BY x.id DESC LIMIT 500',(u['id'],)).fetchall()
        return page('''<h2>طلبات إنهاء حالة العامل</h2><p class="muted">بعد الاعتماد النهائي يُزال العامل من التسكين وتصبح خانته شاغرة، مع حفظ بياناته في سجل مستقل.</p><a class="btn" href="{{url_for('new_worker_exit_request')}}">طلب جديد</a> {% if admin %}<a class="btn btn2" href="{{url_for('worker_exit_archive')}}">سجل العمالة المنتهية</a> <a class="btn btn2" href="{{url_for('export_all_workers')}}">Excel جميع العمالة</a>{% endif %}<div class="tbl-wrap"><table class="tbl"><tr><th>الطلب</th><th>العامل</th><th>الغرفة</th><th>الحالة المطلوبة</th><th>الاعتماد</th><th></th></tr>{% for x in rows %}<tr><td>{{x.request_no}}</td><td>{{x.employee_no}} - {{x.full_name}}</td><td>{{x.zone}} / {{x.room_no}}</td><td>{{types.get(x.exit_type,x.exit_type)}}</td><td>{{x.final_status}}</td><td><a class="btn" href="{{url_for('worker_exit_request_detail',rid=x.id)}}">فتح</a></td></tr>{% else %}<tr><td colspan="6">لا توجد طلبات.</td></tr>{% endfor %}</table></div>''','طلبات إنهاء حالة العامل',u,rows=rows,types=EXIT_TYPES,admin=is_admin(u))

    @app.route('/worker-exit-requests/new',methods=['GET','POST'])
    @login_required
    def new_worker_exit_request():
        u=current_user();allowed=('housing_supervisor','housing_monitor','data_entry','housing_manager','services_manager','super_admin')
        if u['role'] not in allowed:abort(403)
        eno=(request.values.get('employee_no') or '').strip();worker=None;err=''
        with closing(conn()) as c:
            if eno:worker=c.execute('SELECT * FROM workers WHERE employee_no=? AND archived=0',(eno,)).fetchone()
            if request.method=='POST':
                if not worker:err='العامل غير موجود ضمن العمالة الحالية.'
                else:
                    typ=request.form.get('exit_type','');reason=request.form.get('reason','').strip();notes=request.form.get('notes','').strip()
                    if typ not in EXIT_TYPES:err='اختر حالة صحيحة.'
                    elif not reason:err='سبب الطلب مطلوب.'
                    else:
                        no='WEX-'+datetime.utcnow().strftime('%Y%m%d%H%M%S%f')[-16:]
                        def gv(k): return worker[k] if k in worker.keys() else None
                        cur=c.execute('''INSERT INTO worker_exit_requests(request_no,worker_id,employee_no,full_name,iqama_no,nationality,profession,shift_name,rest_day,zone,room_no,exit_type,reason,notes,requested_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',(no,worker['id'],worker['employee_no'],worker['full_name'],gv('iqama_no'),gv('nationality'),gv('profession'),gv('shift_name'),gv('rest_day'),gv('zone'),gv('room_no'),typ,reason,notes,u['id'],now()))
                        audit(c,u,'create','worker_exit_request',cur.lastrowid,{'employee_no':eno,'exit_type':typ});c.commit();return redirect(url_for('worker_exit_request_detail',rid=cur.lastrowid))
        return page('''<div class="card"><h2>طلب إنهاء حالة عامل</h2>{% if err %}<p class="err">{{err}}</p>{% endif %}<form method="get"><label>الرقم الوظيفي</label><input name="employee_no" value="{{eno}}" required><button class="btn">بحث</button></form></div>{% if worker %}<div class="card"><h3>{{worker.employee_no}} - {{worker.full_name}}</h3><p>الغرفة: {{worker.zone}} / {{worker.room_no}} | الوردية: {{worker.shift_name or '-'}} | الراحة: {{worker.rest_day or '-'}}</p><form method="post"><input type="hidden" name="employee_no" value="{{worker.employee_no}}"><label>الحالة</label><select name="exit_type">{% for k,v in types.items() %}<option value="{{k}}">{{v}}</option>{% endfor %}</select><label>السبب</label><textarea name="reason" required></textarea><label>ملاحظات</label><textarea name="notes"></textarea><button class="btn">إرسال الطلب</button></form></div>{% endif %}''','طلب إنهاء حالة عامل',u,eno=eno,worker=worker,types=EXIT_TYPES,err=err)

    @app.route('/worker-exit-requests/<int:rid>',methods=['GET','POST'])
    @login_required
    def worker_exit_request_detail(rid):
        u=current_user()
        with closing(conn()) as c:
            x=c.execute('SELECT x.*,ru.display_name requester FROM worker_exit_requests x LEFT JOIN users ru ON ru.id=x.requested_by WHERE x.id=?',(rid,)).fetchone()
            if not x:abort(404)
            if not is_admin(u) and x['requested_by']!=u['id']:abort(403)
            if request.method=='POST':
                decision=request.form.get('decision');note=request.form.get('decision_reason','').strip()
                if u['role'] in ('housing_manager','super_admin') and x['final_status']=='pending_housing_manager':
                    status='pending_services_manager' if decision=='approved' else 'rejected'
                    c.execute('UPDATE worker_exit_requests SET housing_manager_status=?,housing_manager_by=?,housing_manager_at=?,final_status=? WHERE id=?',(decision,u['id'],now(),status,rid))
                elif u['role'] in ('services_manager','super_admin') and x['final_status']=='pending_services_manager':
                    if decision=='rejected':c.execute("UPDATE worker_exit_requests SET services_manager_status='rejected',services_manager_by=?,services_manager_at=?,final_status='rejected' WHERE id=?",(u['id'],now(),rid))
                    else:
                        w=c.execute('SELECT * FROM workers WHERE id=? AND archived=0',(x['worker_id'],)).fetchone()
                        if not w:abort(400)
                        def gv(k): return w[k] if k in w.keys() else None
                        c.execute('''INSERT INTO worker_exit_archive(source_worker_id,employee_no,full_name,iqama_no,nationality,profession,phone,shift_name,rest_day,department,zone,last_room_no,exit_type,reason,notes,request_no,requested_by,housing_manager_by,services_manager_by,created_at,approved_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',(w['id'],gv('employee_no'),gv('full_name'),gv('iqama_no'),gv('nationality'),gv('profession'),gv('phone'),gv('shift_name'),gv('rest_day'),gv('department'),gv('zone'),gv('room_no'),x['exit_type'],x['reason'],x['notes'],x['request_no'],x['requested_by'],x['housing_manager_by'],u['id'],x['created_at'],now()))
                        c.execute('UPDATE workers SET archived=1,status=?,room_no=NULL,zone=NULL,updated_at=? WHERE id=?',(x['exit_type'],now(),w['id']))
                        c.execute("UPDATE worker_exit_requests SET services_manager_status='approved',services_manager_by=?,services_manager_at=?,final_status='approved',completed_at=? WHERE id=?",(u['id'],now(),now(),rid))
                else:abort(403)
                audit(c,u,decision,'worker_exit_request',rid,{'note':note});c.commit();return redirect(url_for('worker_exit_request_detail',rid=rid))
            can_h=u['role'] in ('housing_manager','super_admin') and x['final_status']=='pending_housing_manager';can_s=u['role'] in ('services_manager','super_admin') and x['final_status']=='pending_services_manager'
        return page('''<div class="card"><h2>{{x.request_no}}</h2><p><b>العامل:</b> {{x.employee_no}} - {{x.full_name}}</p><p><b>الغرفة:</b> {{x.zone}} / {{x.room_no}}</p><p><b>الحالة:</b> {{types.get(x.exit_type)}}</p><p><b>السبب:</b> {{x.reason}}</p><p><b>الاعتماد الحالي:</b> {{x.final_status}}</p>{% if can_h or can_s %}<form method="post"><textarea name="decision_reason" placeholder="ملاحظة"></textarea><button class="btn" name="decision" value="approved">موافقة</button><button class="btn danger" name="decision" value="rejected">رفض</button></form>{% endif %}</div>''','تفاصيل الطلب',u,x=x,types=EXIT_TYPES,can_h=can_h,can_s=can_s)

    @app.get('/worker-exit-archive')
    @login_required
    def worker_exit_archive():
        u=current_user()
        if not is_admin(u):abort(403)
        with closing(conn()) as c:rows=c.execute('SELECT * FROM worker_exit_archive ORDER BY id DESC LIMIT 1000').fetchall()
        return page('''<h2>سجل العمالة المنتهية</h2><a class="btn" href="{{url_for('export_all_workers')}}">Excel جميع العمالة</a><table class="tbl"><tr><th>الرقم</th><th>الاسم</th><th>آخر غرفة</th><th>الحالة</th><th>السبب</th><th>التاريخ</th></tr>{% for x in rows %}<tr><td>{{x.employee_no}}</td><td>{{x.full_name}}</td><td>{{x.zone}} / {{x.last_room_no}}</td><td>{{types.get(x.exit_type,x.exit_type)}}</td><td>{{x.reason}}</td><td>{{x.approved_at}}</td></tr>{% endfor %}</table>''','سجل العمالة المنتهية',u,rows=rows,types=EXIT_TYPES)

    @app.get('/workers/export-all.xlsx')
    @login_required
    def export_all_workers():
        u=current_user()
        if not is_admin(u):abort(403)
        wb=Workbook();ws=wb.active;ws.title='جميع العمالة';ws.append(['الحالة','الرقم الوظيفي','الاسم','الإقامة','الجنسية','المهنة','الجوال','الوردية','الراحة','القسم','الزون','الغرفة','نوع الخروج','سبب الخروج','رقم الطلب','تاريخ الاعتماد'])
        with closing(conn()) as c:
            for w in c.execute('SELECT * FROM workers WHERE archived=0 ORDER BY employee_no').fetchall():
                def gv(k): return w[k] if k in w.keys() else ''
                ws.append(['حالي',gv('employee_no'),gv('full_name'),gv('iqama_no'),gv('nationality'),gv('profession'),gv('phone'),gv('shift_name'),gv('rest_day'),gv('department'),gv('zone'),gv('room_no'),'','','',''])
            for a in c.execute('SELECT * FROM worker_exit_archive ORDER BY id DESC').fetchall():ws.append(['منتهية',a['employee_no'],a['full_name'],a['iqama_no'],a['nationality'],a['profession'],a['phone'],a['shift_name'],a['rest_day'],a['department'],a['zone'],a['last_room_no'],EXIT_TYPES.get(a['exit_type'],a['exit_type']),a['reason'],a['request_no'],a['approved_at']])
        ws.freeze_panes='A2';ws.auto_filter.ref=ws.dimensions
        out=io.BytesIO();wb.save(out);out.seek(0)
        return send_file(out,as_attachment=True,download_name=f'all_workers_{saudi_today().isoformat()}.xlsx',mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
