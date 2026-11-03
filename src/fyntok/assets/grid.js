/* Named functions keep formatting within Dash AG Grid's supported namespace. */
var dagfuncs = window.dashAgGridFunctions = window.dashAgGridFunctions || {};
dagfuncs.fyntokMoney = function (cents) {
    if (cents == null) return '—';
    const magnitude = Math.abs(cents);
    return (cents < 0 ? '-' : '') +
        Math.floor(magnitude / 100).toLocaleString('en-US') + '.' +
        String(magnitude % 100).padStart(2, '0');
};
