from flask import Flask, render_template, request, redirect, url_for, flash, session
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, timedelta
from werkzeug.security import generate_password_hash, check_password_hash
#import pandas as pd
import io
from flask import send_file, jsonify

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///todos.db'
app.config['SECRET_KEY'] = 'your_secret_key'
db = SQLAlchemy(app)

# User Model
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(150), nullable=False, unique=True)
    password = db.Column(db.String(200), nullable=False)
    todos = db.relationship('Todo', backref='user', lazy=True)

# Todo Model
class Todo(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    content = db.Column(db.String(200), nullable=False)
    completed = db.Column(db.Boolean, default=False)
    date_created = db.Column(db.DateTime, default=datetime.utcnow)
    due_date = db.Column(db.DateTime, nullable=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    category = db.Column(db.String(50), nullable=False, default='日常')

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

    reminders = session.pop('reminder', None)
    return render_template('index.html', todos=todos, username=user.username, reminders=reminders, category=category)

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
    session.pop('user_id', None)
    return redirect(url_for('login'))

# Add Todo
@app.route('/add', methods=['POST'])
def add_todo():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    content = request.form['content']  # 獲取待辦事項的內容
    due_date_str = request.form.get('due_date')  # 獲取截止日期（包含時間）
    category = request.form.get('category', '日常')  # 獲取分類，預設為 "日常"
    
    # 將日期字串轉換為 datetime 格式，解析包含時間的格式，否則設為 None
    due_date = datetime.strptime(due_date_str, '%Y-%m-%dT%H:%M') if due_date_str else None
    
    # 建立新的待辦事項
    new_todo = Todo(content=content, due_date=due_date, user_id=session['user_id'], category=category)
    db.session.add(new_todo)  # 新增到資料庫
    db.session.commit()  # 提交變更
    
    return redirect(url_for('index'))  # 重新導向到首頁


# Delete Todo
@app.route('/delete/<int:id>')
def delete_todo(id):
    if 'user_id' not in session:
        return redirect(url_for('login'))
    todo_to_delete = Todo.query.get_or_404(id)
    if todo_to_delete.user_id != session['user_id']:
        flash('無權限刪除此項目')
        return redirect(url_for('index'))
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

    data = request.json
    content = data.get('content')
    due_date_str = data.get('due_date')

    if not content:
        return jsonify({'status': 'error', 'message': '待辦事項內容不能為空'}), 400

    todo.content = content
    todo.due_date = datetime.strptime(due_date_str, '%Y-%m-%dT%H:%M') if due_date_str else None

    try:
        db.session.commit()
        return jsonify({'status': 'success', 'message': '待辦事項已成功更新'})
    except:
        return jsonify({'status': 'error', 'message': '更新失敗'}), 500

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

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(debug=True, port=9453)