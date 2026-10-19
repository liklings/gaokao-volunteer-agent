// 运行时配置。静态站点没有构建步骤，环境变量没法直接注入浏览器 JS，
// 所以用这种最简单的方式：部署到不同环境时，直接改这一个值就行。
// docker-compose 场景下，如果要跑在别的主机名/端口，改这里即可。
window.API_BASE_URL = window.API_BASE_URL || "http://localhost:8000";
