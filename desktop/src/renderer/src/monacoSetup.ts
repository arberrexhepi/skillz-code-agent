import { loader } from '@monaco-editor/react';
import * as monaco from 'monaco-editor/editor/editor.api.js';
import editorWorker from 'monaco-editor/editor/editor.worker.js?worker';
import cssWorker from 'monaco-editor/languages/features/css/css.worker.js?worker';
import htmlWorker from 'monaco-editor/languages/features/html/html.worker.js?worker';
import jsonWorker from 'monaco-editor/languages/features/json/json.worker.js?worker';
import tsWorker from 'monaco-editor/languages/features/typescript/ts.worker.js?worker';
import 'monaco-editor/editor/contrib/find/browser/findController.js';
import 'monaco-editor/languages/features/css/register.js';
import 'monaco-editor/languages/features/html/register.js';
import 'monaco-editor/languages/features/json/register.js';
import 'monaco-editor/languages/features/typescript/register.js';
import 'monaco-editor/languages/definitions/cpp/register.js';
import 'monaco-editor/languages/definitions/csharp/register.js';
import 'monaco-editor/languages/definitions/go/register.js';
import 'monaco-editor/languages/definitions/java/register.js';
import 'monaco-editor/languages/definitions/markdown/register.js';
import 'monaco-editor/languages/definitions/php/register.js';
import 'monaco-editor/languages/definitions/python/register.js';
import 'monaco-editor/languages/definitions/ruby/register.js';
import 'monaco-editor/languages/definitions/rust/register.js';
import 'monaco-editor/languages/definitions/shell/register.js';
import 'monaco-editor/languages/definitions/sql/register.js';
import 'monaco-editor/languages/definitions/yaml/register.js';

self.MonacoEnvironment = {
  getWorker(_moduleId: string, label: string): Worker {
    if (label === 'json') return new jsonWorker();
    if (label === 'css' || label === 'scss' || label === 'less') return new cssWorker();
    if (label === 'html' || label === 'handlebars' || label === 'razor') return new htmlWorker();
    if (label === 'typescript' || label === 'javascript') return new tsWorker();
    return new editorWorker();
  },
};

loader.config({ monaco });
