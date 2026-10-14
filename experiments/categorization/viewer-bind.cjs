// Promptfoo 0.124.1's viewer does not expose a bind-address option.
// Bind its numeric-port listen calls to all IPv4 interfaces for remote access.
const { Server } = require('node:net');
const listen = Server.prototype.listen;
Server.prototype.listen = function (port, ...args) {
  if (typeof port === 'number' && (args.length === 0 || typeof args[0] === 'function')) {
    return Reflect.apply(listen, this, [port, '0.0.0.0', ...args]);
  }
  return Reflect.apply(listen, this, [port, ...args]);
};
