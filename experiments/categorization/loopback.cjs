// Promptfoo 0.124.1's viewer does not expose a bind-address option.
// Restrict its numeric-port listen calls to loopback for local private datasets.
const { Server } = require('node:net');
const listen = Server.prototype.listen;
Server.prototype.listen = function (port, ...args) {
  if (typeof port === 'number' && (args.length === 0 || typeof args[0] === 'function')) {
    return Reflect.apply(listen, this, [port, '127.0.0.1', ...args]);
  }
  return Reflect.apply(listen, this, [port, ...args]);
};
