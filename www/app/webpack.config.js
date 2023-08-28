const webpack = require("webpack");
const glob = require("glob");
const path = require("path");
const { https } = require("./https");

module.exports = {
  devServer: {
    static: path.join(__dirname, "..", "public"),
    server: {
      type: "https",
      options: {
        ca: "./certs/root.crt",
        key: "./certs/www.key",
        cert: "./certs/www.crt",
      },
    },
    port: 8443,
  },
  entry: {
    // Get each file from the 'components' directory and use its name as the entry point name.
    ...glob.sync("./src/components/**/*.tsx").reduce((acc, path) => {
      const entry = path.replace("./src/components/", "").replace(".tsx", "");
      acc[entry] = path;
      return acc;
    }, {}),
  },
  output: {
    filename: "[name].js", // Use the entry point name as the output file name
    path: path.resolve(__dirname, "dist"),
    publicPath: "/dist/",
  },
  resolve: {
    extensions: [".ts", ".tsx", ".js", ".jsx"],
  },
  module: {
    rules: [
      {
        test: /\.(ts|tsx)$/,
        exclude: /node_modules/,
        use: {
          loader: "babel-loader",
          options: {
            presets: [
              "@babel/preset-env",
              "@babel/preset-react",
              "@babel/preset-typescript",
            ],
            plugins: ["syntax-dynamic-import"],
          },
        },
      },
      {
        test: /\.css$/,
        use: ["style-loader", "css-loader"],
      },
    ],
  },
};
