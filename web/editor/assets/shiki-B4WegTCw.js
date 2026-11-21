function e(){return Promise.resolve({getLoadedLanguages(){return[]},codeToHtml(){throw new Error("离线构建未包含语法高亮引擎，代码按纯文本显示。")}})}export{e as createHighlighter};
