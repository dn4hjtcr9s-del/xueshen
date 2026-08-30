// 社区子视图导航包装器（§九/D43）：page==="community" 时的内部状态导航，
// 不引入 URL 路由。子视图状态保留在组件 state 中；未登录写操作经
// onLoginRequired 跳转 profile，登录后由 App 切回 community（本组件保持挂载）。
import { useCallback, useEffect, useState } from "react";
import { useAuth } from "../../auth/AuthContext";
import {
  getCommunityPermissions,
  listBoards,
  type CommunityBoard,
  type CommunityPermissions,
} from "../../api/community";
import CommunityHome from "./CommunityHome";
import BoardDetail from "./BoardDetail";
import PostDetail from "./PostDetail";
import CreatePost from "./CreatePost";
import BoardApplication from "./BoardApplication";
import AdminApplications from "./AdminApplications";

export type CommunityPageKey = "home" | "board" | "post" | "create" | "apply" | "admin";

export { CommunityHome, BoardDetail, PostDetail, CreatePost, BoardApplication, AdminApplications };

type CommunityRoute = {
  view: CommunityPageKey;
  boardSlug?: string;
  postId?: string;
  createBoardId?: string;
};

const HOME_ROUTE: CommunityRoute = { view: "home" };

export function CommunityPage({
  targetPostId,
  onTargetConsumed,
  onLoginRequired,
}: {
  targetPostId?: string | null;
  onTargetConsumed?: () => void;
  onLoginRequired?: () => void;
}) {
  const { user } = useAuth();
  const isLoggedIn = user !== null;
  const [route, setRoute] = useState<CommunityRoute>(HOME_ROUTE);
  const [boards, setBoards] = useState<CommunityBoard[]>([]);
  const [boardsLoading, setBoardsLoading] = useState(true);
  const [permissions, setPermissions] = useState<CommunityPermissions | null>(null);

  // 板块列表在包装层加载，供首页宫格与发帖视图共用（记住来源板块）。
  // 每次回到 home 视图都刷新：审核建吧、发帖后 post_count 变化等需要生效
  const reloadBoards = useCallback(() => {
    let cancelled = false;
    setBoardsLoading(true);
    listBoards()
      .then((items) => {
        if (!cancelled) {
          setBoards(items);
          setBoardsLoading(false);
        }
      })
      .catch(() => {
        if (!cancelled) {
          setBoards([]);
          setBoardsLoading(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (route.view !== "home") return;
    const cancel = reloadBoards();
    return cancel;
  }, [route.view, reloadBoards]);

  // 管理员入口按 permissions 结果显示（D23）；匿名不请求
  useEffect(() => {
    if (!isLoggedIn) {
      setPermissions(null);
      return;
    }
    let cancelled = false;
    getCommunityPermissions()
      .then((p) => {
        if (!cancelled) setPermissions(p);
      })
      .catch(() => {
        if (!cancelled) setPermissions(null);
      });
    return () => {
      cancelled = true;
    };
  }, [isLoggedIn]);

  const loginRequired = useCallback(() => {
    onLoginRequired?.();
  }, [onLoginRequired]);

  // §6.5：通知点击 → 打开帖子详情；打开后消费 target
  useEffect(() => {
    if (targetPostId) {
      setRoute({ view: "post", postId: targetPostId });
      onTargetConsumed?.();
    }
  }, [targetPostId, onTargetConsumed]);

  const goHome = useCallback(() => setRoute(HOME_ROUTE), []);
  const goBoard = useCallback((slug: string) => setRoute({ view: "board", boardSlug: slug }), []);
  const goPost = useCallback((postId: string) => {
    // 从板块详情进入时记住来源板块，详情返回时回板块而非首页
    setRoute((prev) => ({ view: "post", postId, boardSlug: prev.boardSlug }));
  }, []);

  const goCreate = useCallback(
    (boardId?: string) => {
      if (!isLoggedIn) {
        loginRequired();
        return;
      }
      setRoute({ view: "create", createBoardId: boardId });
    },
    [isLoggedIn, loginRequired],
  );

  const goApply = useCallback(() => {
    if (!isLoggedIn) {
      loginRequired();
      return;
    }
    setRoute({ view: "apply" });
  }, [isLoggedIn, loginRequired]);

  const goAdmin = useCallback(() => {
    if (!isLoggedIn) {
      loginRequired();
      return;
    }
    setRoute({ view: "admin" });
  }, [isLoggedIn, loginRequired]);

  const finishCreate = useCallback(() => {
    const sourceBoard = boards.find((b) => b.board_id === route.createBoardId);
    if (sourceBoard) setRoute({ view: "board", boardSlug: sourceBoard.slug });
    else setRoute(HOME_ROUTE);
  }, [boards, route.createBoardId]);

  switch (route.view) {
    case "board":
      return (
        <BoardDetail
          slug={route.boardSlug ?? ""}
          onBack={goHome}
          onOpenPost={goPost}
          onCreatePost={goCreate}
          isLoggedIn={isLoggedIn}
          onLoginRequired={loginRequired}
        />
      );
    case "post":
      return (
        <PostDetail
          postId={route.postId ?? ""}
          onBack={() => {
            if (route.boardSlug) setRoute({ view: "board", boardSlug: route.boardSlug });
            else setRoute(HOME_ROUTE);
          }}
          isLoggedIn={isLoggedIn}
          onLoginRequired={loginRequired}
        />
      );
    case "create":
      return (
        <CreatePost
          boardId={route.createBoardId}
          boards={boards}
          onDone={finishCreate}
          onCancel={() => {
            const sourceBoard = boards.find((b) => b.board_id === route.createBoardId);
            if (sourceBoard) setRoute({ view: "board", boardSlug: sourceBoard.slug });
            else setRoute(HOME_ROUTE);
          }}
          isLoggedIn={isLoggedIn}
          onLoginRequired={loginRequired}
        />
      );
    case "apply":
      return (
        <BoardApplication
          onBack={goHome}
          isLoggedIn={isLoggedIn}
          onLoginRequired={loginRequired}
        />
      );
    case "admin":
      return (
        <AdminApplications
          onBack={goHome}
          isAdmin={permissions?.is_community_admin ?? false}
        />
      );
    default:
      return (
        <CommunityHome
          boards={boards}
          boardsLoading={boardsLoading}
          onOpenBoard={goBoard}
          onOpenPost={goPost}
          onCreatePost={goCreate}
          onApply={goApply}
          onAdmin={goAdmin}
          isAdmin={permissions?.is_community_admin ?? false}
          isLoggedIn={isLoggedIn}
          onLoginRequired={loginRequired}
        />
      );
  }
}

export default CommunityPage;
