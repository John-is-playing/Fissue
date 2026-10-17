# 沙盒镜像：给「Fissue 维护自己」这类**依赖不轻**的仓库用。
#
# 基础镜像 `docker/sandbox/Dockerfile` 只带了运行时（python/node/go/java）与 pytest 插件，
# 而一个真实项目的测试套件还需要它**自己的依赖**（sqlalchemy / fastapi / respx …）。
# 沙盒默认断网（`sandbox.network: none`），容器里现装是装不上的，所以必须在**构建期**装好。
#
# 构建（上下文是仓库根）：
#   docker build -f docker/self/Dockerfile -t fissue/self-base:latest .
# 然后：
#   config.yaml → sandbox.image: fissue/self-base:latest
#
# 自检（把当前仓库挂进去跑它自己的测试，这是「沙盒环境是否可用」的判据）：
#   docker run --rm --network none -v "$PWD":/workspace -w /workspace \
#     fissue/self-base:latest python -m pytest -q

FROM fissue/sandbox-base:latest

# ① 装**依赖**：运行时依赖 + dev/test 依赖（respx 等）。
#    刻意**不装 fissue 本身**——装了的话容器里会 import 到镜像内的那份旧副本，而不是
#    挂载进来的当前工作区，验证器就测不到这次要验的代码了。
#    依赖清单直接从 pyproject 读，避免两处清单漂移（往 dev 组加测试依赖后重建镜像即可）。
WORKDIR /build
COPY pyproject.toml ./
RUN pip install --no-cache-dir $(python -c "import tomllib; p=tomllib.load(open('pyproject.toml','rb'))['project']; print(' '.join(list(p.get('dependencies', [])) + list(p.get('optional-dependencies', {}).get('dev', []))))")

# ② 让 `import fissue` 命中**挂载进来的源码**：沙盒恒把工作区挂在 /workspace，
#    而项目是 src 布局（`src/fissue`），PYTHONPATH 先于 site-packages 生效。
ENV PYTHONPATH=/workspace/src

WORKDIR /workspace
