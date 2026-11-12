import json,os,time
rows=[]
def pytest_runtest_logreport(report):
 if report.when in ('setup','call','teardown'):rows.append({'nodeid':report.nodeid,'phase':report.when,'duration':report.duration,'outcome':report.outcome})
def pytest_sessionfinish(session,exitstatus):
 if not os.getenv('REPLAY_METRICS_OUTPUT'):return
 with open(os.environ['REPLAY_METRICS_OUTPUT'],'w') as f:json.dump({'exitstatus':int(exitstatus),'rows':rows},f)
