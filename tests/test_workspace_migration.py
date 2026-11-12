from pathlib import Path
from alembic.migration import MigrationContext
from alembic.autogenerate import compare_metadata
from flask_migrate import upgrade,downgrade
from backend.app import create_app
from backend.database import db

def test_independent_baseline_upgrade_and_downgrade(tmp_path):
 app=create_app({'TESTING':True,'SQLALCHEMY_DATABASE_URI':'sqlite:///'+str(tmp_path/'migration.db')})
 with app.app_context():
  upgrade(directory=str(Path(__file__).resolve().parents[1]/'migrations'))
  with db.engine.connect() as conn:
   assert compare_metadata(MigrationContext.configure(conn),db.metadata)==[]
   from sqlalchemy import text
   schema=" ".join(row[0] or "" for row in conn.execute(text("SELECT sql FROM sqlite_master")))
   assert not any(term in schema.lower() for term in ("lextext","lexintel","cognito","word_addin","word-addin"))
  downgrade(directory=str(Path(__file__).resolve().parents[1]/'migrations'),revision='base')
  from sqlalchemy import inspect
  assert inspect(db.engine).get_table_names()==['alembic_version']


def test_local_identity_cleanup_preserves_existing_account_and_owned_data(tmp_path):
 from sqlalchemy import inspect,text
 from werkzeug.security import generate_password_hash,check_password_hash
 root=Path(__file__).resolve().parents[1]
 app=create_app({'TESTING':True,'SECRET_KEY':'migration-test-key',
     'SQLALCHEMY_DATABASE_URI':'sqlite:///'+str(tmp_path/'existing-owner.db')})
 password='A local account migration test passphrase'
 digest=generate_password_hash(password)
 with app.app_context():
  upgrade(directory=str(root/'migrations'),revision='0002_local_auth')
  with db.engine.begin() as conn:
   conn.execute(text("INSERT INTO user (id,email,password_hash,contact_info,is_disabled) VALUES (1,:email,:digest,'',0)"),
       {'email':'owner@example.test','digest':digest})
   conn.execute(text('INSERT INTO local_auth_bootstrap (id,user_id) VALUES (1,1)'))
   conn.execute(text("INSERT INTO projects (id,name,userId) VALUES (1,'Preserved project',1)"))
  client=app.test_client()
  with client.session_transaction() as session:session['user_id']=1
  upgrade(directory=str(root/'migrations'))
  from backend.models import User,LocalAuthBootstrap,Project
  owner=db.session.get(User,1)
  assert User.query.count()==1 and owner.email=='owner@example.test' and owner.password_hash==digest
  assert check_password_hash(owner.password_hash,password)
  assert db.session.get(LocalAuthBootstrap,1).user_id==1 and db.session.get(Project,1).userId==1
  schema=' '.join(str(c) for c in inspect(db.engine).get_columns('user'))
  assert not any(word in schema.lower() for word in ('cognito','lextext','lexintel','word_addin'))
  assert client.get('/api/workspace/projects').status_code==200
  # The migration round trip also preserves the local owner and project.
  downgrade(directory=str(root/'migrations'),revision='0002_local_auth')
  upgrade(directory=str(root/'migrations'))
  db.session.expire_all()
  assert db.session.get(User,1).password_hash==digest
  assert db.session.get(Project,1).name=='Preserved project'
