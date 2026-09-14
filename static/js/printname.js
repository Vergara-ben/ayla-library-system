// Print with a chosen file name. Browsers name "Save as PDF" after the page title.
(function () {
    window.printWithName = function (inputId) {
        var field = document.getElementById(inputId);
        var name = ((field && field.value) || '').replace(/[\\/:*?"<>|]+/g, '').replace(/\.pdf$/i, '').trim();
        var original = document.title;
        if (name) document.title = name;
        var restore = function () {
            document.title = original;
            window.removeEventListener('afterprint', restore);
        };
        window.addEventListener('afterprint', restore);
        window.print();
    };
})();
