// Test-only entry point: production index.js and local authentication are never changed.
const path = require('path'),
  fs = require('fs'),
  webpack = require('webpack'),
  express = require('express');
const frontend = path.resolve(__dirname, '..'),
  root = path.resolve(frontend, '..');
process.env.NODE_ENV = 'test';
const compiler = webpack({
  mode: 'development',
  devtool: false,
  context: frontend,
  entry: path.join(root, 'tests/e2e/browser/entry.jsx'),
  output: {
    path: path.join(root, 'artifacts/workspace-replay/bundle'),
    filename: 'replay.js',
    publicPath: '/'
  },
  resolve: {
    extensions: ['.js', '.jsx', '.json'],
    modules: [path.join(frontend, 'node_modules'), 'node_modules']
  },
  resolveLoader: {
    modules: [path.join(frontend, 'node_modules'), 'node_modules']
  },
  module: {
    rules: [{
      test: /\.[jt]sx?$/,
      exclude: /node_modules/,
      use: {
        loader: 'babel-loader',
        options: {
          babelrc: false,
          configFile: false,
          presets: [[require.resolve('@babel/preset-react'), {
            runtime: 'automatic'
          }]]
        }
      }
    }, {
      test: /\.css$/,
      use: ['style-loader', 'css-loader']
    }]
  },
  plugins: [new webpack.DefinePlugin({
    'process.env.NODE_ENV': JSON.stringify('test')
  }), new webpack.NormalModuleReplacementPlugin(/[/\\]auth[/\\]useSetHeaders(?:\.js)?$/, r => {
    r.request = path.join(root, 'tests/e2e/browser/auth-boundary.js');
  }), new webpack.NormalModuleReplacementPlugin(/[/\\]Constants(?:\.js)?$/, r => {
    r.request = path.join(root, 'tests/e2e/browser/constants.js');
  })],
  stats: 'errors-warnings'
});
const app = express();
app.use(require('webpack-dev-middleware')(compiler, {
  stats: 'errors-warnings'
}));
app.get('/', (req, res) => res.type('html').send('<!doctype html><html><head><title>Workspace replay</title></head><body><div id="root"></div><script src="/replay.js"></script></body></html>'));
const server = app.listen(Number(process.env.WORKSPACE_REPLAY_PORT || 39240), '127.0.0.1', () => console.log('Workspace replay server listening'));
function stop() {
  server.close();
  compiler.close(() => process.exit());
}
process.on('SIGTERM', stop);
process.on('SIGINT', stop);
