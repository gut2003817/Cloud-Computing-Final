from flask import Flask, render_template, request, redirect, url_for, flash, session
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, timedelta
from werkzeug.security import generate_password_hash, check_password_hash
import pandas as pd
import io
from flask import send_file, jsonify
import os
from werkzeug.utils import secure_filename
from google.cloud import storage
import os

GCS_BUCKET = os.getenv('GCS_BUCKET')
GCS_KEY_FILE = os.getenv('GCS_KEY_FILE')

# 初始化 GCS 客戶端
storage_client = storage.Client.from_service_account_json(GCS_KEY_FILE)
bucket = storage_client.bucket(GCS_BUCKET)


app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///todos.db'
app.config['SECRET_KEY'] = 'your_secret_key'
db = SQLAlchemy(app)
UPLOAD_FOLDER = 'uploads'
if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

app.permanent_session_lifetime = timedelta(minutes=10)

# User Model
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(150), nullable=False, unique=True)
    password = db.Column(db.String(200), nullable=False)
    todos = db.relationship('Todo', backref='user', lazy=True)

# File Model
class File(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(200), nullable=False)  # 檔案名稱
    filepath = db.Column(db.String(300), nullable=False)  # 檔案路徑
    todo_id = db.Column(db.Integer, db.ForeignKey('todo.id'), nullable=False)  # 關聯待辦事項
    upload_time = db.Column(db.DateTime, default=datetime.utcnow)  # 上傳時間

# 更新 Todo 模型以支持關聯
class Todo(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    content = db.Column(db.String(200), nullable=False)
    completed = db.Column(db.Boolean, default=False)
    date_created = db.Column(db.DateTime, default=datetime.utcnow)
    due_date = db.Column(db.DateTime, nullable=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    category = db.Column(db.String(50), nullable=False, default='日常')
    files = db.relationship('File', backref='todo', lazy=True)  # 關聯檔案

# Index Page - Show Todos
@app.route('/')
def index():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    user = User.query.get(session['user_id'])
    if not user:
        return redirect(url_for('login'))

    # 獲取分類參數，默認為 "all"
    category = request.args.get('category', 'all')

    # 根據分類篩選事項
    if category == 'completed':
        todos = Todo.query.filter_by(user_id=user.id, completed=True).order_by(Todo.due_date.asc().nulls_last(), Todo.date_created.asc()).all()
    elif category == 'incomplete':
        todos = Todo.query.filter_by(user_id=user.id, completed=False).order_by(Todo.due_date.asc().nulls_last(), Todo.date_created.asc()).all()
    else:  # 默認顯示全部
        todos = Todo.query.filter_by(user_id=user.id).order_by(Todo.due_date.asc().nulls_last(), Todo.date_created.asc()).all()

    # 添加過期標記
    for todo in todos:
        todo.is_overdue = todo.due_date and todo.due_date < datetime.utcnow()

    files = File.query.join(Todo, File.todo_id == Todo.id).filter(Todo.user_id == user.id).all()

    reminders = session.pop('reminder', None)
    return render_template('index.html', todos=todos, username=user.username, reminders=reminders, category=category, files=files)

# Register
@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        existing_user = User.query.filter_by(username=username).first()
        if existing_user:
            flash('使用者名稱已存在')
            return redirect(url_for('register'))
        hashed_password = generate_password_hash(password)
        new_user = User(username=username, password=hashed_password)
        db.session.add(new_user)
        db.session.commit()
        flash('註冊成功，請登入')
        return redirect(url_for('login'))
    return render_template('register.html')

# Login
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        user = User.query.filter_by(username=username).first()
        if user and check_password_hash(user.password, password):
            session['user_id'] = user.id

            # 查找即將到期的事項（2 天內截止）
            upcoming_deadlines = Todo.query.filter(
                Todo.user_id == user.id,
                Todo.due_date != None,
                Todo.due_date <= datetime.utcnow() + timedelta(days=2),
                Todo.due_date > datetime.utcnow()
            ).all()

            if upcoming_deadlines:
                session['reminder'] = [
                    f"{todo.content} 截止日期為 {todo.due_date.strftime('%Y-%m-%d %H:%M:%S')}"
                    for todo in upcoming_deadlines
                ]
            else:
                session.pop('reminder', None)

            return redirect(url_for('index'))
        
        flash('使用者名稱或密碼錯誤，或是帳號未註冊')
    return render_template('login.html')

# Logout
@app.route('/logout')
def logout():
    session.clear()
    session.pop('user_id', None)
    return redirect(url_for('login'))

#auto-logout
@app.route('/auto-logout', methods=['POST'])
def auto_logout():
    session.pop('user_id', None)  # 清除 session
    return jsonify({'status': 'logged_out'})

@app.before_request
def update_last_activity():
    if 'user_id' in session:
        session.permanent = True  # 設定 session 為永久，以便配合 `permanent_session_lifetime`
        session.modified = True  # 確保活動時間更新

@app.before_request
def update_last_activity():
    if 'user_id' in session:
        session.permanent = True  # 設定 session 為永久，以便配合 `permanent_session_lifetime`
        session.modified = True  # 確保活動時間更新

@app.route('/add', methods=['POST'])
def add_todo():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    # 獲取表單資料
    content = request.form['content']
    due_date_str = request.form.get('due_date')
    category = request.form.get('category', '日常')
    due_date = datetime.strptime(due_date_str, '%Y-%m-%dT%H:%M') if due_date_str else None

    # 建立待辦事項
    new_todo = Todo(content=content, due_date=due_date, user_id=session['user_id'], category=category)
    db.session.add(new_todo)
    db.session.commit()

    # 處理上傳檔案到 Google Cloud Storage
    if 'files' in request.files:
        for file in request.files.getlist('files'):
            if file and file.filename:
                # GCS 上傳
                filename = secure_filename(file.filename)
                blob = bucket.blob(filename)
                blob.upload_from_file(file)
                file_url = f"https://storage.googleapis.com/{GCS_BUCKET}/{filename}"

                # 儲存到資料庫
                new_file = File(filename=filename, filepath=file_url, todo_id=new_todo.id)
                db.session.add(new_file)
        db.session.commit()

    return redirect(url_for('index'))


@app.route('/manage_files', methods=['POST'])
def manage_files():
    if 'user_id' not in session:
        flash("請先登入", "danger")
        return redirect(url_for('login'))
    
    # 刪除選擇的檔案
    if request.form.get('action') == 'delete':
        file_ids = request.form.getlist('delete_files')  # 獲取所有選中的檔案ID
        if file_ids:
            for file_id in file_ids:
                file_to_delete = File.query.get(file_id)
                if file_to_delete and file_to_delete.todo.user_id == session['user_id']:
                    try:
                        # 刪除實體檔案
                        os.remove(file_to_delete.filepath)
                        db.session.delete(file_to_delete)
                    except FileNotFoundError:
                        flash(f"檔案 {file_to_delete.filename} 不存在", "warning")
            db.session.commit()
            flash("檔案已刪除", "delete")
        else:
            flash("未選擇任何檔案", "warning")
        return redirect(url_for('files_page'))

    # 新增檔案到待辦事項
    elif request.form.get('action') == 'upload':
        todo_id = request.form.get('todo_id')
        files = request.files.getlist('files')

        if not todo_id:
            flash("請選擇待辦事項", "warning")
            return redirect(url_for('files_page'))

        todo = Todo.query.get(todo_id)
        if not todo or todo.user_id != session['user_id']:
            flash("無權限新增到該待辦事項", "danger")
            return redirect(url_for('files_page'))

        if not files:
            flash("未選擇任何檔案", "warning")
            return redirect(url_for('files_page'))

        for file in files:
            if file and file.filename:
                filename = secure_filename(file.filename)
                filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                file.save(filepath)

                new_file = File(filename=filename, filepath=filepath, todo_id=todo.id)
                db.session.add(new_file)

        db.session.commit()
        flash("檔案已成功新增", "add")
        return redirect(url_for('files_page'))

    # 如果請求中沒有正確的動作
    flash("未知的操作", "danger")
    return redirect(url_for('files_page'))


@app.route('/files', methods=['GET', 'POST'])
def files_page():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    user_id = session['user_id']
    category = request.args.get('category')
    todo_id = request.args.get('todo_id')

    # 篩選待辦事項
    todos_query = Todo.query.filter_by(user_id=user_id)
    if category:
        todos_query = todos_query.filter_by(category=category)
    todos = todos_query.all()

    # 篩選檔案
    files_query = File.query.join(Todo).filter(Todo.user_id == user_id)
    if todo_id:
        files_query = files_query.filter(File.todo_id == todo_id)
    files = files_query.all()

    # 判斷是否無檔案
    no_files_message = None
    if not files:
        no_files_message = "尚未新增檔案"

    return render_template(
        'files.html',
        files=files,
        todos=todos,
        selected_category=category,
        selected_todo_id=todo_id,
        no_files_message=no_files_message
    )

@app.route('/get_todos', methods=['GET'])
def get_todos():
    if 'user_id' not in session:
        return jsonify([])

    category = request.args.get('category')
    user_id = session['user_id']

    todos_query = Todo.query.filter_by(user_id=user_id)
    if category:
        todos_query = todos_query.filter_by(category=category)

    todos = todos_query.all()
    todos_dict = [{"id": todo.id, "content": todo.content} for todo in todos]
    return jsonify(todos_dict)

@app.route('/get_files', methods=['GET'])
def get_files():
    if 'user_id' not in session:
        return jsonify([])

    user_id = session['user_id']
    category = request.args.get('category')
    todo_id = request.args.get('todo_id')

    files_query = File.query.join(Todo).filter(Todo.user_id == user_id)

    if category:
        files_query = files_query.filter(Todo.category == category)
    if todo_id:
        files_query = files_query.filter(File.todo_id == todo_id)

    files = files_query.all()
    files_list = [{
        "id": file.id,
        "filename": file.filename,
        "category": file.todo.category,
        "content": file.todo.content
    } for file in files]

    return jsonify(files_list)

# Delete Todo
@app.route('/delete/<int:id>')
def delete_todo(id):
    if 'user_id' not in session:
        return redirect(url_for('login'))
    todo_to_delete = Todo.query.get_or_404(id)
    if todo_to_delete.user_id != session['user_id']:
        flash('無權限刪除此項目')
        return redirect(url_for('index'))

    # 先刪除與此待辦事項相關的檔案記錄與實體檔案
    for f in todo_to_delete.files:
        if os.path.exists(f.filepath):
            os.remove(f.filepath)
        db.session.delete(f)
    
    db.session.delete(todo_to_delete)
    db.session.commit()
    return redirect(url_for('index'))

@app.route('/api/edit/<int:id>', methods=['POST'])
def api_edit_todo(id):
    if 'user_id' not in session:
        return jsonify({'status': 'error', 'message': '未登入'}), 403

    todo = Todo.query.get_or_404(id)
    if todo.user_id != session['user_id']:
        return jsonify({'status': 'error', 'message': '無權限編輯此項目'}), 403

    content = request.form.get('content')
    due_date_str = request.form.get('due_date')

    if not content:
        return jsonify({'status': 'error', 'message': '待辦事項內容不能為空'}), 400

    todo.content = content
    todo.due_date = datetime.strptime(due_date_str, '%Y-%m-%dT%H:%M') if due_date_str else None

    # 欲刪除的檔案ID列表
    delete_files = request.form.getlist('delete_files')  # ex: ['1', '2']
    for fid in delete_files:
        file_to_delete = File.query.get(int(fid))
        if file_to_delete and file_to_delete.todo_id == todo.id:
            if os.path.exists(file_to_delete.filepath):
                os.remove(file_to_delete.filepath)
            db.session.delete(file_to_delete)

    # 新增上傳的檔案(若有)
    new_files = request.files.getlist('files')
    for f in new_files:
        if f and f.filename:
            filename = secure_filename(f.filename)
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            f.save(filepath)
            new_file = File(filename=filename, filepath=filepath, todo_id=todo.id)
            db.session.add(new_file)

    try:
        db.session.commit()
        return jsonify({'status': 'success', 'message': '待辦事項已成功更新'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': f'更新失敗: {str(e)}'}), 500

    # 新增上傳的檔案（若有）
    new_files = request.files.getlist('files')
    for f in new_files:
        if f and f.filename:
            filename = secure_filename(f.filename)
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            f.save(filepath)
            new_file = File(filename=filename, filepath=filepath, todo_id=todo.id)
            db.session.add(new_file)

    try:
        db.session.commit()
        return jsonify({'status': 'success', 'message': '待辦事項已成功更新'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': f'更新失敗: {str(e)}'}), 500

@app.route('/category/<string:category>', methods=['GET', 'POST'])
def category_page(category):
    if 'user_id' not in session:
        return redirect(url_for('login'))

    user = User.query.get(session['user_id'])
    if not user:
        return redirect(url_for('login'))

    todos = Todo.query.filter_by(user_id=user.id, category=category).order_by(Todo.due_date.asc().nulls_last(), Todo.date_created.asc()).all()

    if request.method == 'POST':
        content = request.form['content']
        due_date_str = request.form.get('due_date')
        # 使用正確的解析格式
        due_date = datetime.strptime(due_date_str, '%Y-%m-%dT%H:%M') if due_date_str else None
        new_todo = Todo(content=content, due_date=due_date, user_id=user.id, category=category)
        db.session.add(new_todo)
        db.session.commit()
        return redirect(url_for('category_page', category=category))

    return render_template('category.html', todos=todos, username=user.username, category=category)


@app.route('/complete/<int:id>')
def complete_todo(id):
    if 'user_id' not in session:
        return redirect(url_for('login'))
    todo = Todo.query.get_or_404(id)
    if todo.user_id != session['user_id']:
        flash('無權限更新此項目')
        return redirect(url_for('index'))
    todo.completed = not todo.completed
    db.session.commit()
    return redirect(url_for('index'))

@app.route('/export')
def export():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    todos = Todo.query.filter_by(user_id=session['user_id']).order_by(Todo.due_date.asc().nulls_last(), Todo.date_created.asc()).all()
    data = [{
        '日期': todo.due_date.strftime('%Y-%m-%d %H:%M:%S') if todo.due_date else '',
        '待辦事項': todo.content,
        '完成狀態': '已完成' if todo.completed else '未完成',
        '是否截止': '已截止' if todo.due_date and todo.due_date < datetime.utcnow() else '未截止'
    } for todo in todos]

    df = pd.DataFrame(data)
    output = io.BytesIO()

    with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
        df.to_excel(writer, index=False, sheet_name='待辦事項')

    output.seek(0)

    return send_file(
        output,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        download_name='todos.xlsx',
        as_attachment=True
    )

@app.route('/upload_file/<int:todo_id>', methods=['POST'])
def upload_file(todo_id):
    if 'user_id' not in session:
        return jsonify({'status': 'error', 'message': '未登入'}), 403
    
    todo = Todo.query.get_or_404(todo_id)
    if todo.user_id != session['user_id']:
        return jsonify({'status': 'error', 'message': '無權限'}), 403

    if 'files' in request.files:
        for file in request.files.getlist('files'):
            if file and file.filename:
                filename = secure_filename(file.filename)
                filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                file.save(filepath)

                new_file = File(filename=filename, filepath=filepath, todo_id=todo_id)
                db.session.add(new_file)
        db.session.commit()
        return jsonify({'status': 'success', 'message': '檔案已上傳'})

    return jsonify({'status': 'error', 'message': '無檔案'}), 400

@app.route('/delete_file/<int:file_id>', methods=['DELETE'])
def delete_file(file_id):
    if 'user_id' not in session:
        return jsonify({'status': 'error', 'message': '未登入'}), 403

    file = File.query.get_or_404(file_id)
    if file.todo.user_id != session['user_id']:
        return jsonify({'status': 'error', 'message': '無權限'}), 403

    os.remove(file.filepath)  # 刪除實體檔案
    db.session.delete(file)
    db.session.commit()
    return jsonify({'status': 'success', 'message': '檔案已刪除'})

@app.route('/view_file/<int:file_id>')
def view_file(file_id):
    if 'user_id' not in session:
        flash("請先登入", "danger")
        return redirect(url_for('login'))

    file = File.query.get(file_id)
    if not file or file.todo.user_id != session['user_id']:
        flash("無權限檢視該檔案", "danger")
        return redirect(url_for('files_page'))

    # 確認檔案存在
    if not os.path.exists(file.filepath):
        flash("檔案不存在", "danger")
        return redirect(url_for('files_page'))

    return send_file(file.filepath)

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(debug=True, port=9453)