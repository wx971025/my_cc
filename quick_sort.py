"""
快速排序（Quick Sort）实现
============================
快速排序是一种基于"分治法"的高效排序算法。
核心思路：
  1. 从数组中选取一个"基准元素"（pivot）
  2. 将所有小于基准的元素放到基准左侧，大于基准的放到右侧（分区操作）
  3. 对左右两个子数组递归执行相同操作，直到子数组长度为 0 或 1

时间复杂度：
  - 平均情况：O(n log n)
  - 最坏情况：O(n²)（数组已有序时，每次选到最大/最小值作为基准）
  - 最好情况：O(n log n)
空间复杂度：O(log n)（递归调用栈）
稳定性：不稳定排序
"""


def quick_sort(arr):
    """
    快速排序入口函数（原地排序版本）

    参数：
        arr (list): 待排序的列表

    返回：
        list: 排序后的列表（同一对象，原地修改）
    """
    if len(arr) <= 1:
        # 递归终止条件：空数组或只有一个元素时，无需排序
        return arr

    _quick_sort_helper(arr, 0, len(arr) - 1)
    return arr


def _quick_sort_helper(arr, low, high):
    """
    快速排序递归辅助函数

    参数：
        arr  (list): 待排序的列表
        low  (int) : 当前子数组的起始索引
        high (int) : 当前子数组的结束索引
    """
    if low < high:
        # 1. 对当前子数组进行分区，获取基准元素最终所在的位置
        pivot_index = _partition(arr, low, high)

        # 2. 递归排序基准左侧的子数组
        _quick_sort_helper(arr, low, pivot_index - 1)

        # 3. 递归排序基准右侧的子数组
        _quick_sort_helper(arr, pivot_index + 1, high)


def _partition(arr, low, high):
    """
    分区函数（Lomuto 分区方案）

    选取子数组最右边的元素作为基准（pivot），
    将子数组重新排列，使得：
      - 所有 < pivot 的元素位于 pivot 左侧
      - 所有 > pivot 的元素位于 pivot 右侧
    最终将 pivot 放置到它"应在"的排序位置并返回该索引。

    参数：
        arr  (list): 列表
        low  (int) : 子数组起始索引
        high (int) : 子数组结束索引（同时也是 pivot 的初始位置）

    返回：
        int: pivot 最终所在的索引
    """
    pivot = arr[high]   # 以最右边的元素作为基准
    i = low - 1         # i 指向"小于 pivot 区域"的最后一个元素

    for j in range(low, high):
        # 遍历 low ~ high-1 的每个元素
        if arr[j] <= pivot:
            # 发现比 pivot 小（或相等）的元素，将其交换到左侧区域
            i += 1
            arr[i], arr[j] = arr[j], arr[i]

    # 将 pivot 放到正确位置（i+1），即左侧全部 <= pivot，右侧全部 > pivot
    arr[i + 1], arr[high] = arr[high], arr[i + 1]

    return i + 1    # 返回 pivot 最终所在的索引


def quick_sort_simple(arr):
    """
    快速排序——简洁递归版（新建列表，便于理解核心思想）

    与原地排序版本相比，此版本每次递归都会创建新列表，
    空间开销更大（O(n log n)），但代码更直观，适合学习理解。

    参数：
        arr (list): 待排序的列表

    返回：
        list: 排序后的新列表
    """
    if len(arr) <= 1:
        # 递归终止条件
        return arr

    pivot = arr[len(arr) // 2]               # 选取中间元素作为基准

    left   = [x for x in arr if x < pivot]  # 所有小于基准的元素
    middle = [x for x in arr if x == pivot] # 所有等于基准的元素
    right  = [x for x in arr if x > pivot]  # 所有大于基准的元素

    # 递归排序左右两侧，再拼接
    return quick_sort_simple(left) + middle + quick_sort_simple(right)


# ============================================================
# 测试示例
# ============================================================
if __name__ == "__main__":
    print("=" * 50)
    print("       快速排序（Quick Sort）测试")
    print("=" * 50)

    # ------ 测试 1：普通无序数组 ------
    arr1 = [64, 25, 12, 22, 11]
    print(f"\n【测试 1】普通无序数组")
    print(f"  排序前：{arr1}")
    quick_sort(arr1)
    print(f"  排序后：{arr1}")

    # ------ 测试 2：包含重复元素 ------
    arr2 = [3, 6, 8, 10, 1, 2, 1, 6, 3]
    print(f"\n【测试 2】包含重复元素")
    print(f"  排序前：{arr2}")
    quick_sort(arr2)
    print(f"  排序后：{arr2}")

    # ------ 测试 3：已升序排列 ------
    arr3 = [1, 2, 3, 4, 5]
    print(f"\n【测试 3】已升序排列")
    print(f"  排序前：{arr3}")
    quick_sort(arr3)
    print(f"  排序后：{arr3}")

    # ------ 测试 4：已降序排列 ------
    arr4 = [5, 4, 3, 2, 1]
    print(f"\n【测试 4】已降序排列")
    print(f"  排序前：{arr4}")
    quick_sort(arr4)
    print(f"  排序后：{arr4}")

    # ------ 测试 5：单个元素 / 空数组 ------
    arr5 = [42]
    arr6 = []
    print(f"\n【测试 5】单个元素 & 空数组")
    print(f"  单元素排序前：{arr5}  ->  排序后：{quick_sort(arr5)}")
    print(f"  空数组  排序前：{arr6}  ->  排序后：{quick_sort(arr6)}")

    # ------ 测试 6：负数与混合数组 ------
    arr7 = [-3, 7, 0, -1, 5, -8, 4]
    print(f"\n【测试 6】含负数的混合数组")
    print(f"  排序前：{arr7}")
    quick_sort(arr7)
    print(f"  排序后：{arr7}")

    # ------ 测试 7：简洁版对比 ------
    arr8 = [38, 27, 43, 3, 9, 82, 10]
    print(f"\n【测试 7】简洁递归版（新建列表）")
    print(f"  排序前：{arr8}")
    result = quick_sort_simple(arr8)
    print(f"  排序后：{result}")

    print("\n" + "=" * 50)
    print("           所有测试完成!")
    print("=" * 50)
