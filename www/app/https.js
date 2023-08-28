const fs = require("fs");

const readSSLFile = (filename) => {
  console.log(`Reading ${filename}`);
  try {
    return fs.readFileSync(filename);
  } catch (err) {
    console.error(`Error reading ${filename}: `, err);
    return null;
  }
};

const https = {
  key: readSSLFile("./certs/www.key"),
  cert: readSSLFile("./certs/www.crt"),
  ca: readSSLFile("./certs/root.crt"),
};

module.exports = https;
