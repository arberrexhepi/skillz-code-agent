const assert = require('node:assert/strict');
const { test } = require('node:test');
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
const load = require('./load-ts.cjs');
const { findFileReferences, resolveFileReference, PathText, FileNavigationContext, terminalFileLinks } = load(() => ({
  ...require('../src/shared/fileReferences.ts'), ...require('../src/renderer/src/components/PathText.tsx'), ...require('../src/renderer/src/terminalFileLinks.ts'),
}));
test('prose finds repository, Windows, quoted Unicode and line references without swallowing punctuation', () => {
  const text = 'Read /repo/src/app.ts:12:3, `docs/My kërkesë.md` and "C:\\work\\src\\app.ts". Next (src/test.py#L9C2), src/main.ts(7,4).';
  const matches = findFileReferences(text);
  assert.deepEqual(matches.map(x => x.text), ['/repo/src/app.ts:12:3', 'docs/My kërkesë.md', 'C:\\work\\src\\app.ts', 'src/test.py#L9C2', 'src/main.ts(7,4)']);
  for (const match of matches) assert.equal(text.slice(match.start, match.end), match.text);
});
test('URLs, emails, metadata keys and ordinary slash-separated words are not files', () => {
  assert.deepEqual(findFileReferences('https://example.com/src/app.ts www.example.com/foo.md user@example.com facts.storage foo/bar architecture/goal 1.2.3'), []);
  assert.deepEqual(findFileReferences('README.md .gitignore Dockerfile repo_facts.md').map(x=>x.text), ['README.md','.gitignore','Dockerfile','repo_facts.md']);
});
test('references resolve within Windows and Unix workspaces and preserve line/column', () => {
  for (const raw of ['/repo/src/app.ts:12:3','C:\\WORK\\src\\app.ts:12:3','src/./app.ts:12:3','src/nested/../app.ts:12:3','file:///C:/work/src/app.ts#L12C3']) {
    assert.deepEqual(resolveFileReference(raw,'C:\\work'),{path:'src/app.ts',line:12,column:3});
  }
  assert.deepEqual(resolveFileReference('/home/me/repo/src/app.py','/home/me/repo'),{path:'src/app.py'});
  assert.deepEqual(resolveFileReference('file:///C:/work/docs/My%20k%C3%ABrkes%C3%AB.md','C:/work'),{path:'docs/My kërkesë.md'});
});
test('navigation rejects traversal, other roots, UNC targets, URLs and alternate data streams', () => {
  for (const raw of ['../secret.txt','/repo/../secret.txt','C:/work/../secret.txt','C:/work-other/x.ts','D:/work/x.ts','/etc/passwd','\\\\server\\share\\a.ts','https://example.com/a.ts','file://server/share/a.ts','file:///C:/work/../../secret.txt','src/a.ts:payload','src/a.ts\u0000']) assert.equal(resolveFileReference(raw,'C:/work'),null,raw);
});
test('prose rendering preserves text and escapes stored values while leaving inputs and literal code alone', () => {
  const content = React.createElement(PathText,null,React.createElement('p',null,'Open src/a.ts:3 then continue <img onerror=bad>. '),React.createElement('pre',null,'cat src/a.ts'),React.createElement('button',null,'Run src/a.ts'),React.createElement('textarea',{defaultValue:'src/a.ts'}));
  const html=renderToStaticMarkup(React.createElement(FileNavigationContext.Provider,{value:{root:'C:/work',open(){}}},content));
  assert.equal((html.match(/class="path-chip"/g)||[]).length,1);
  assert.match(html,/Open file src\/a.ts:3/);
  assert.match(html,/then continue &lt;img/);
  assert.match(html,/<pre>cat src\/a.ts<\/pre>/);
  assert.match(html,/<button>Run src\/a.ts<\/button>/);
});
test('terminal links use terminal-cell positions across wrapping and Unicode, then open the correct location', () => {
  const rows=[['界','', ' ', ...'/repo/src/a.'], [...'ts:12:3']];
  const terminal={buffer:{active:{length:2,getLine:(i)=>i>=2?undefined:{isWrapped:i===1,length:rows[i].length,getCell:(x)=>({getWidth:()=>rows[i][x]==='界'?2:rows[i][x]===''?0:1,getChars:()=>rows[i][x]})}}}};
  const calls=[];
  const links=terminalFileLinks(terminal,2,'C:/work',ref=>calls.push(ref));
  assert.equal(links.length,1);
  assert.deepEqual(links[0].range,{start:{x:4,y:1},end:{x:7,y:2}});
  links[0].activate();
  assert.deepEqual(calls,[{path:'src/a.ts',line:12,column:3}]);
});

test('editor exposes save UI, keeps its shortcut current, and reveals lines without replacing edited content', (t) => {
  const Module = require('node:module');
  const original=Module._load;
  const refs=[];let refIndex=0;let effects=[];let shellCommand;
  const previousWindow=globalThis.window;const previousDocument=globalThis.document;
  globalThis.window={workbench:{editor:{onCommand:(listener)=>{shellCommand=listener;return ()=>{};}}}};
  globalThis.document={execCommand:(command)=>calls.push(`document:${command}`)};
  t.after(()=>{globalThis.window=previousWindow;globalThis.document=previousDocument;});
  const fakeReact={...React,useRef:(value)=>refs[refIndex++] ||= {current:value},useEffect:(effect)=>effects.push(effect)};
  const FakeEditor=()=>null;
  t.mock.method(Module,'_load',function(request,...rest){ if(request==='react')return fakeReact; if(request==='@monaco-editor/react')return {__esModule:true,default:FakeEditor}; return original.call(this,request,...rest); });
  const {EditorPane}=load(()=>require('../src/renderer/src/components/EditorPane.tsx'));
  let active={id:'file:src/app.ts',kind:'file',title:'app.ts',document:{path:'src/app.ts',language:'typescript',content:'saved'},content:'unsaved edits',dirty:true,reveal:{line:12,column:3,request:1}};
  const calls=[];let saveCommand;
  const editor={getModel:()=>({validatePosition:(p)=>p}),setPosition:p=>calls.push(p),revealPositionInCenter(){},focus(){},hasTextFocus:()=>true,trigger:(_source,command)=>calls.push(command),addCommand(_key,callback){saveCommand=callback;}};
  const monaco={KeyMod:{CtrlCmd:1},KeyCode:{KeyS:2},editor:{setModelMarkers(){}}};
  let onSave=()=>{};
  function render() {refIndex=0;effects=[];return EditorPane({tabs:[active],activeId:active.id,diagnostics:{},onActivate(){},onClose(){},onChange(){},onSave});}
  const findEditor=(node)=>{if(!React.isValidElement(node))return null;if(node.type===FakeEditor)return node;return React.Children.toArray(node.props.children).map(findEditor).find(Boolean);};
  const findSave=(node)=>{if(!React.isValidElement(node))return null;if(node.props.className==='editor-save')return node;return React.Children.toArray(node.props.children).map(findSave).find(Boolean);};
  const findCommand=(node,label)=>{if(!React.isValidElement(node))return null;if(node.props['aria-label']===label)return node;return React.Children.toArray(node.props.children).map(child=>findCommand(child,label)).find(Boolean);};
  const saves=[];onSave=id=>saves.push(`button:${id}`);let tree=render();let element=findEditor(tree);
  assert.equal(element.props.value,'unsaved edits');
  const save=findSave(tree);assert.equal(save.props.disabled,false);save.props.onClick();assert.deepEqual(saves,['button:file:src/app.ts']);
  element.props.onMount(editor,monaco);
  assert.deepEqual(calls.at(-1),{lineNumber:12,column:3});
  active={...active,reveal:{line:8,column:1,request:2}};
  onSave=id=>saves.push(`shortcut:${id}`);tree=render();element=findEditor(tree);effects.forEach(effect=>effect());saveCommand();
  assert.deepEqual(calls.at(-1),{lineNumber:8,column:1});
  findCommand(tree,'Undo').props.onClick();findCommand(tree,'Redo').props.onClick();shellCommand('undo');
  assert.deepEqual(saves,['button:file:src/app.ts','shortcut:file:src/app.ts']);
  assert.equal(element.props.value,'unsaved edits');
  assert.deepEqual(calls.filter(value=>typeof value==='string'),['undo','redo','undo']);
});
