(function () {
  const desiredLabels = ["Guides", "Features", "Architecture"];

  function getSidebarRoot() {
    return document.querySelector(".sidebar");
  }

  function findLabelAnchor(sidebarRoot, label) {
    const anchors = sidebarRoot.querySelectorAll("a");

    return Array.from(anchors).find(
      (anchor) => anchor.textContent && anchor.textContent.trim() === label,
    );
  }

  function getAncestors(node, stopNode) {
    const ancestors = [];
    let current = node;

    while (current) {
      ancestors.push(current);

      if (current === stopNode) {
        break;
      }

      current = current.parentElement;
    }

    return ancestors;
  }

  function getDeepestCommonAncestor(nodes, stopNode) {
    if (nodes.length === 0) {
      return null;
    }

    const ancestorSets = nodes.map((node) => new Set(getAncestors(node, stopNode)));
    const firstAncestors = getAncestors(nodes[0], stopNode);

    return firstAncestors.find((ancestor) =>
      ancestorSets.every((ancestorSet) => ancestorSet.has(ancestor)),
    );
  }

  function getImmediateChildUnderAncestor(node, ancestor) {
    let current = node;

    while (current && current.parentElement !== ancestor) {
      current = current.parentElement;
    }

    return current;
  }

  function reorderSidebar() {
    const sidebarRoot = getSidebarRoot();

    if (!sidebarRoot) {
      return;
    }

    const anchors = desiredLabels
      .map((label) => findLabelAnchor(sidebarRoot, label))
      .filter(Boolean);

    if (anchors.length !== desiredLabels.length) {
      return;
    }

    const commonAncestor = getDeepestCommonAncestor(anchors, sidebarRoot);

    if (!commonAncestor) {
      return;
    }

    const orderedNodes = desiredLabels
      .map((label) => findLabelAnchor(sidebarRoot, label))
      .map((anchor) => getImmediateChildUnderAncestor(anchor, commonAncestor))
      .filter(Boolean);

    if (orderedNodes.length !== desiredLabels.length) {
      return;
    }

    orderedNodes.forEach((node) => {
      commonAncestor.appendChild(node);
    });
  }

  function installSidebarOrdering() {
    reorderSidebar();

    const sidebarRoot = getSidebarRoot();

    if (!sidebarRoot) {
      return;
    }

    const observer = new MutationObserver(() => {
      reorderSidebar();
    });

    observer.observe(sidebarRoot, {
      childList: true,
      subtree: true,
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", installSidebarOrdering, { once: true });
  } else {
    installSidebarOrdering();
  }
})();
