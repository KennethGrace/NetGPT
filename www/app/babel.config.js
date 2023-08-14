module.exports = function(api) {
  api.cache(true);

  const presets = [
    "@babel/preset-env",
    "@babel/preset-react",
    "@babel/preset-typescript",
  ];

  const plugins = ["@babel/plugin-syntax-dynamic-import"];

  return {
    presets,
    plugins,
  };
};
