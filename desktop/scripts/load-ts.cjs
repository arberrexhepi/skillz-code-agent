const fs = require('node:fs');
const { transformSync } = require('esbuild');

module.exports = function loadTypeScript(load) {
  const extensions = ['.ts', '.tsx'];
  const original = extensions.map((extension) => require.extensions[extension]);
  for (const extension of extensions) {
    require.extensions[extension] = (module, filename) => {
      const output = transformSync(fs.readFileSync(filename, 'utf8'), {
        format: 'cjs', target: 'node22', loader: extension === '.tsx' ? 'tsx' : 'ts', jsx: 'automatic', sourcefile: filename,
      });
      module._compile(output.code, filename);
    };
  }
  try { return load(); }
  finally {
    extensions.forEach((extension, index) => {
      if (original[index]) require.extensions[extension] = original[index];
      else delete require.extensions[extension];
    });
  }
};
