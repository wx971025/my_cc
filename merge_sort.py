"""
归并排序（Merge Sort）
原理：分治思想，将列表不断二分，分到单个元素后再逐层合并，
      合并时保持有序。
时间复杂度：O(n log n)
空间复杂度：O(n)
"""


def merge_sort(arr):
    """
    归并排序函数（递归实现）
    :param arr: 待排序列表
    :return: 排序后的新列表
    """
    # 递归终止条件：列表长度为0或1时，已经有序
    if len(arr) <= 1:
        return arr

    # 找到中间位置，将列表一分为二
    mid = len(arr) // 2
    left_half = arr[:mid]
    right_half = arr[mid:]

    # 递归地对左右两部分进行归并排序
    left_sorted = merge_sort(left_half)
    right_sorted = merge_sort(right_half)

    # 合并两个有序列表
    return _merge(left_sorted, right_sorted)


def _merge(left, right):
    """
    合并两个有序列表
    :param left: 左侧有序列表
    :param right: 右侧有序列表
    :return: 合并后的有序列表
    """
    merged = []
    i = j = 0

    # 依次比较两个列表的头部元素，将较小的加入结果
    while i < len(left) and j < len(right):
        if left[i] <= right[j]:
            merged.append(left[i])
            i += 1
        else:
            merged.append(right[j])
            j += 1

    # 将剩余元素追加到结果末尾
    merged.extend(left[i:])
    merged.extend(right[j:])

    return merged


# ---- 测试示例 ----
if __name__ == "__main__":
    test_data = [38, 27, 43, 3, 9, 82, 10]
    print(f"排序前：{test_data}")
    result = merge_sort(test_data)
    print(f"排序后：{result}")

    # 测试含重复元素的列表
    dup_data = [5, 3, 8, 3, 1, 5, 2]
    print(f"\n排序前：{dup_data}")
    print(f"排序后：{merge_sort(dup_data)}")

    # 测试空列表
    empty = []
    print(f"\n排序前：{empty}")
    print(f"排序后：{merge_sort(empty)}")
