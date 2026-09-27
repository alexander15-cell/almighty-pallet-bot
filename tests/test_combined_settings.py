import json
from pathlib import Path
import pytest
from combined_settings import SetupError, check_state, initialize, load_settings

def example(tmp_path):
    value = json.loads((Path(__file__).parents[1] / 'setup.example.json').read_text())
    value.update(operatorUserIds=['100000000000000001'], sourceId='a'*32)
    value['roleIds'] = {'Pallet Admin':'100000000000000002', 'Queue Review':'100000000000000003', 'Listing Management':'100000000000000004'}
    file = tmp_path / 'setup.json'
    file.write_text(json.dumps(value))
    return file, value

def test_example_no_secrets_or_live_switches():
    value = json.loads((Path(__file__).parents[1] / 'setup.example.json').read_text())
    assert not value['botToken'] and not value['websiteSecret'] and not value['sourceId']
    assert not value['publishEnabled'] and not value['cutoverConfirmed']

@pytest.mark.parametrize('change', [
    {'operatorUserIds':[]}, {'operatorUserIds':['100000000000000001']*2},
    {'sourceId':'bad'}, {'publishEnabled':'true'}, {'pollSeconds':True},
    {'pollSeconds':0}, {'guildId':1549900260724183044},
])
def test_bad_config(tmp_path, change):
    file,value=example(tmp_path)
    value.update(change)
    file.write_text(json.dumps(value))
    with pytest.raises(SetupError): load_settings(file)

def test_send_requires_both_switches_and_secret(tmp_path):
    file,value=example(tmp_path)
    value['botToken']='x'*40
    value['websiteSecret']='a'*64
    value['publishEnabled']=True
    file.write_text(json.dumps(value))
    with pytest.raises(SetupError): load_settings(file,connect=True,send=True)
    value['cutoverConfirmed']=True
    file.write_text(json.dumps(value))
    settings=load_settings(file,connect=True,send=True)
    assert settings.website=='https://4guys1palletoverstock.com'

def test_initialize_refuses_existing_directory(tmp_path):
    file,_=example(tmp_path)
    settings=load_settings(file)
    settings.state_directory.mkdir()
    marker=settings.state_directory/'keep.txt'
    marker.write_text('unchanged')
    with pytest.raises(SetupError): initialize(settings)
    assert marker.read_text()=='unchanged'

def test_missing_state_does_not_create_database(tmp_path):
    file,_=example(tmp_path)
    settings=load_settings(file)
    with pytest.raises(SetupError): check_state(settings)
    assert not settings.state_directory.exists()

def test_duplicate_keys_fail(tmp_path):
    file,_=example(tmp_path)
    file.write_text('{"schemaVersion":1,"schemaVersion":1}')
    with pytest.raises(SetupError): load_settings(file)

def test_initialization_isolated_in_subprocess(tmp_path):
    import subprocess,sys
    file,_=example(tmp_path)
    root=Path(__file__).parents[1]
    result=subprocess.run([sys.executable,str(root/'combined_bot.py'),'--settings',str(file),'--initialize'],capture_output=True,text=True)
    assert result.returncode==0, result.stdout+result.stderr
    result=subprocess.run([sys.executable,str(root/'combined_bot.py'),'--settings',str(file)],capture_output=True,text=True)
    assert result.returncode==0, result.stdout+result.stderr
    import sqlite3
    with sqlite3.connect(tmp_path/'state'/'intake.sqlite') as db:
        assert db.execute("SELECT seq FROM sqlite_sequence WHERE name='items'").fetchone()[0]==999999999
        assert db.execute('SELECT COUNT(*) FROM items').fetchone()[0]==0
    result=subprocess.run([sys.executable,str(root/'combined_bot.py'),'--settings',str(file),'--initialize'],capture_output=True,text=True)
    assert result.returncode==1
