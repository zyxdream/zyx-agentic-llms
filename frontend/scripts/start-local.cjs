const config=require('../../config/dev-monica.json');
process.env.PORT=String(config.frontendPort);
process.env.HOST='127.0.0.1';
process.env.BROWSER='none';
process.env.WDS_SOCKET_PORT=String(config.frontendPort);
// CRA's automatic proxy can leave the LAN host undefined when binding loopback.
// Keep an explicit host allowlist instead of disabling host checks.
const serverConfigPath=require.resolve('react-scripts/config/webpackDevServer.config');
const serverConfig=require(serverConfigPath);
require.cache[serverConfigPath].exports=(proxy,allowedHost)=>({...serverConfig(proxy,allowedHost||'localhost'),allowedHosts:['localhost','127.0.0.1']});
require('react-scripts/scripts/start');
