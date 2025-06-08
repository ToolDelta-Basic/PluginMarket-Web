// 防调试保护
setInterval(function() {
    const check = function() {};
    check.toString = function() {
        return "debugger";
    };
    console.log('%c', check);
}, 1000);