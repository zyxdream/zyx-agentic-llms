const fs=require('fs'),path=require('path');
const root=path.resolve(__dirname,'../..');
if(process.env.REACT_APP_ENVIRONMENT&&process.env.REACT_APP_ENVIRONMENT!=='dev-monica')throw new Error('Only dev-monica is supported');
const config=JSON.parse(fs.readFileSync(path.join(root,'config/dev-monica.json'),'utf8'));
fs.writeFileSync(path.join(root,'frontend/src/localConfig.generated.json'),JSON.stringify(config,null,2));
